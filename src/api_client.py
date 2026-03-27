"""API client for book creation and page upload."""

import asyncio
import logging
import time
from typing import Dict, List, Optional

import aiohttp
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type


class APIClient:
    """Handles API calls to book and page endpoints."""

    def __init__(self, config, logger: logging.Logger):
        """
        Initialize APIClient.

        Args:
            config: Configuration object
            logger: Logger instance
        """
        self.config = config
        self.logger = logger
        self.base_url = config.api_base_url.rstrip('/')        
        self.max_retries = config.max_retries

    def _get_retry_decorator(self):
        """Get retry decorator with configured parameters."""
        return retry(
            stop=stop_after_attempt(self.max_retries),
            wait=wait_exponential(multiplier=self.config.retry_backoff, min=1, max=60),
            retry=retry_if_exception_type((aiohttp.ClientError, aiohttp.ServerTimeoutError)),
            reraise=True
        )

    def _get_authorization_header_value(self) -> Optional[str]:
        """Return the Authorization header value for protected endpoints."""
        sl_api_secret = getattr(self.config, "sl_api_secret", None)
        if not sl_api_secret:
            return None

        # If the secret already includes a scheme (e.g. "Bearer ..."), pass through.
        if " " in sl_api_secret.strip():
            return sl_api_secret.strip()

        return f"Bearer {sl_api_secret.strip()}"

    async def create_book(self, metadata: Dict, max_retries: int = 3) -> Optional[str]:
        """
        Create a book via API with retry logic.

        Expected API response format:
        {
          "id": "book_id",
          "tenant_id": "default",
          ...other fields...
        }

        Args:
            metadata: Book metadata dictionary
            max_retries: Maximum number of retry attempts (default: 3)

        Returns:
            Book ID from API response, or None if failed after all retries
        """
        url = f"{self.base_url}/api/books"

        auth_value = self._get_authorization_header_value()
        if not auth_value:
            self.logger.error("SL_API_SECRET is missing; cannot call POST /api/books")
            return None

        # Map metadata fields to API expected format
        payload = {
            "title": metadata.get('Title') or 'Untitled',
            "author": metadata.get('Author') or 'Unknown',
            "language": metadata.get('Language') or 'Unknown',
            "published": metadata.get('Year of publication') or 'Unknown',
            "publisher": metadata.get('Publisher'),
            "place_of_publication": metadata.get('Place of publication'),
            "printer": metadata.get('Printer'),
            "image_source": {
                "provider": 'efm',
                "provider_name": "Embassy of the Free Mind",
                "license": "publicdomain"
            }
        }

        for attempt in range(1, max_retries + 1):
            try:
                if attempt > 1:
                    self.logger.info(f"Retry attempt {attempt}/{max_retries} for creating book")

                self.logger.info(f"Creating book: {metadata.get('picturae_barcode')} (attempt {attempt}/{max_retries})")
                self.logger.debug(f"POST {url} with payload: {payload}")

                timeout = aiohttp.ClientTimeout(total=30)
                async with aiohttp.ClientSession(timeout=timeout, headers={'User-Agent': 'SourceLibrary-MCP/'}) as session:
                    async with session.post(
                        url,
                        json=payload,
                        headers={
                            'Content-Type': 'application/json',
                            'Authorization': auth_value,
                        }
                    ) as response:
                        if response.status == 201:
                            data = await response.json()
                            book_id = data.get('id')
                            if attempt > 1:
                                self.logger.info(f"Successfully created book on attempt {attempt}/{max_retries}")
                            self.logger.info(f"Successfully created book with ID: {book_id}")
                            return book_id
                        else:
                            text = await response.text()
                            self.logger.error(f"Failed to create book (attempt {attempt}/{max_retries}). Status: {response.status}, Response: {text}")

                            # Check if we should retry
                            if attempt < max_retries:
                                wait_time = 2 ** (attempt + 1)
                                self.logger.info(f"Waiting {wait_time} seconds before retry...")
                                await asyncio.sleep(wait_time)
                                continue
                            else:
                                return None

            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                self.logger.error(f"Error creating book (attempt {attempt}/{max_retries}): {e}")

                if attempt < max_retries:
                    wait_time = 2 ** (attempt + 1)
                    self.logger.info(f"Waiting {wait_time} seconds before retry...")
                    await asyncio.sleep(wait_time)
                    continue
                else:
                    return None

            except Exception as e:
                self.logger.error(f"Unexpected error creating book (attempt {attempt}/{max_retries}): {e}")
                return None

        return None
            

    async def upload_page_from_s3(self, book_id: str, s3_url: str, max_retries: int = 3) -> Optional[str]:
        """
        Upload page image from S3 presigned URL with retry logic.
        API downloads directly from S3 without intermediate temp file.

        Args:
            book_id: Book ID from create_book response
            s3_url: S3 presigned HTTPS URL
            max_retries: Maximum number of retry attempts (default: 3)

        Returns:
            Page ID from API response, or None if failed after all retries
        """
        url = f"{self.base_url}/api/upload/from-s3"

        for attempt in range(1, max_retries + 1):
            try:
                if attempt > 1:
                    self.logger.info(f"Retry attempt {attempt}/{max_retries} for uploading page from S3")

                self.logger.debug(f"Uploading page from S3 link for book {book_id} (attempt {attempt}/{max_retries})")

                # Send JSON payload (NOT form-data)
                payload = {
                    "bookId": book_id,
                    "imageUrls": [s3_url]  # Single-item array
                }

                timeout = aiohttp.ClientTimeout(total=300)
                async with aiohttp.ClientSession(timeout=timeout, headers={'User-Agent': 'SourceLibrary-MCP/'}) as session:
                    async with session.post(url, json=payload) as response:
                        # Handle success responses: 200 (full success) or 207 (partial success)
                        if response.status in [200, 207]:
                            data = await response.json()
                            if data.get('success') and data.get('pages') and len(data['pages']) > 0:
                                page_id = data['pages'][0].get('id')
                                page_number = data['pages'][0].get('page_number')

                                # Log if partial success (future-proofing for batch uploads)
                                if response.status == 207 and data.get('errors'):
                                    self.logger.warning(
                                        f"Partial success (207): Page uploaded but API reported errors: {data.get('errors')}"
                                    )

                                if attempt > 1:
                                    self.logger.info(f"Successfully uploaded page from S3 on attempt {attempt}/{max_retries}")
                                self.logger.debug(f"Successfully uploaded page from S3 (page_number={page_number}), ID: {page_id}")
                                return page_id
                            else:
                                self.logger.error(f"Upload response missing page data: {data}")
                                # Don't retry for malformed responses
                                return None
                        else:
                            text = await response.text()
                            self.logger.error(f"Failed to upload page from S3 (attempt {attempt}/{max_retries}). Status: {response.status}, Response: {text}")

                            # Check if we should retry
                            if attempt < max_retries:
                                # Calculate exponential backoff: 2^(attempt+1) seconds
                                wait_time = 2 ** (attempt + 1)
                                self.logger.info(f"Waiting {wait_time} seconds before retry...")
                                await asyncio.sleep(wait_time)
                                continue
                            else:
                                # All retries exhausted
                                return None

            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                self.logger.error(f"Error uploading page from S3 (attempt {attempt}/{max_retries}): {e}")

                # Check if we should retry
                if attempt < max_retries:
                    # Calculate exponential backoff: 2^(attempt+1) seconds
                    wait_time = 2 ** (attempt + 1)
                    self.logger.info(f"Waiting {wait_time} seconds before retry...")
                    await asyncio.sleep(wait_time)
                    continue
                else:
                    # All retries exhausted
                    return None

            except Exception as e:
                self.logger.error(f"Unexpected error uploading page from S3 (attempt {attempt}/{max_retries}): {e}")
                # Don't retry for unexpected errors
                return None

        return None

    async def update_book_after_upload(self, book_id: str, max_retries: int = 3) -> bool:
        """
        Notify API that all pages for a book have been uploaded with retry logic.
        Called after all pages are successfully uploaded.

        Args:
            book_id: Book ID from create_book response
            max_retries: Maximum number of retry attempts (default: 3)

        Returns:
            True if successful, False if failed after all retries
        """
        url = f"{self.base_url}/api/upload/update-book"

        for attempt in range(1, max_retries + 1):
            try:
                if attempt > 1:
                    self.logger.info(f"Retry attempt {attempt}/{max_retries} for updating book after upload")

                self.logger.debug(f"Updating book after upload completion: {book_id} (attempt {attempt}/{max_retries})")

                # Send JSON payload
                payload = {
                    "bookId": book_id
                }

                timeout = aiohttp.ClientTimeout(total=30)
                async with aiohttp.ClientSession(timeout=timeout, headers={'User-Agent': 'SourceLibrary-MCP/'}) as session:
                    async with session.post(url, json=payload) as response:
                        if response.status == 200:
                            if attempt > 1:
                                self.logger.info(f"Successfully updated book on attempt {attempt}/{max_retries}")
                            self.logger.info(f"Successfully updated book {book_id} after upload")
                            return True
                        else:
                            text = await response.text()
                            self.logger.error(f"Failed to update book after upload (attempt {attempt}/{max_retries}). Status: {response.status}, Response: {text}")

                            if attempt < max_retries:
                                wait_time = 2 ** (attempt + 1)
                                self.logger.info(f"Waiting {wait_time} seconds before retry...")
                                await asyncio.sleep(wait_time)
                                continue
                            else:
                                return False

            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                self.logger.error(f"Error updating book after upload (attempt {attempt}/{max_retries}): {e}")

                if attempt < max_retries:
                    wait_time = 2 ** (attempt + 1)
                    self.logger.info(f"Waiting {wait_time} seconds before retry...")
                    await asyncio.sleep(wait_time)
                    continue
                else:
                    return False

            except Exception as e:
                self.logger.error(f"Unexpected error updating book after upload (attempt {attempt}/{max_retries}): {e}")
                return False

        return False

    async def verify_book(self, book_id: str, max_retries: int = 3) -> Optional[Dict]:
        """
        Verify book exists and get its details with retry logic.
        Used for data integrity verification after migration.

        Args:
            book_id: Book ID to verify
            max_retries: Maximum number of retry attempts (default: 3)

        Returns:
            Book details dictionary or None if not found after all retries
        """
        url = f"{self.base_url}/api/books/{book_id}"

        for attempt in range(1, max_retries + 1):
            try:
                if attempt > 1:
                    self.logger.info(f"Retry attempt {attempt}/{max_retries} for verifying book")

                self.logger.debug(f"Verifying book {book_id} (attempt {attempt}/{max_retries})")

                timeout = aiohttp.ClientTimeout(total=30)
                async with aiohttp.ClientSession(timeout=timeout, headers={'User-Agent': 'SourceLibrary-MCP/'}) as session:
                    async with session.get(url) as response:
                        if response.status == 200:
                            if attempt > 1:
                                self.logger.info(f"Successfully verified book on attempt {attempt}/{max_retries}")
                            return await response.json()
                        else:
                            self.logger.warning(f"Book {book_id} verification failed (attempt {attempt}/{max_retries}). Status: {response.status}")

                            if attempt < max_retries:
                                wait_time = 2 ** (attempt + 1)
                                self.logger.info(f"Waiting {wait_time} seconds before retry...")
                                await asyncio.sleep(wait_time)
                                continue
                            else:
                                return None

            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                self.logger.error(f"Error verifying book {book_id} (attempt {attempt}/{max_retries}): {e}")

                if attempt < max_retries:
                    wait_time = 2 ** (attempt + 1)
                    self.logger.info(f"Waiting {wait_time} seconds before retry...")
                    await asyncio.sleep(wait_time)
                    continue
                else:
                    return None

            except Exception as e:
                self.logger.error(f"Unexpected error verifying book {book_id} (attempt {attempt}/{max_retries}): {e}")
                return None

        return None