"""API client for book creation and page upload."""

import logging
import time
from typing import Dict, List, Optional

import requests
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
            retry=retry_if_exception_type((requests.exceptions.RequestException, requests.exceptions.Timeout)),
            reraise=True
        )

    def create_book(self, metadata: Dict) -> Optional[str]:
        """
        Create a book via API.

        Expected API response format:
        {
          "id": "book_id",
          "tenant_id": "default",
          ...other fields...
        }

        Args:
            metadata: Book metadata dictionary

        Returns:
            Book ID from API response, or None if failed
        """
        url = f"{self.base_url}/api/books"

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

        try:
            self.logger.info(f"Creating book: {metadata.get('picturae_barcode')}")
            self.logger.debug(f"POST {url} with payload: {payload}")

            response = requests.post(
                url,
                json=payload,
                headers={'Content-Type': 'application/json'},
                timeout=30
            )

            if response.status_code == 201:
                data = response.json()
                book_id = data.get('id')
                self.logger.info(f"Successfully created book with ID: {book_id}")
                return book_id
            else:
                self.logger.error(f"Failed to create book. Status: {response.status_code}, Response: {response.text}")
                return None

        except requests.exceptions.RequestException as e:
            self.logger.error(f"Error creating book: {e}")
            return None
            

    def upload_page_from_s3(self, book_id: str, s3_url: str) -> Optional[str]:
        """
        Upload page image from S3 presigned URL.
        API downloads directly from S3 without intermediate temp file.

        Args:
            book_id: Book ID from create_book response
            s3_url: S3 presigned HTTPS URL

        Returns:
            Page ID from API response, or None if failed
        """
        url = f"{self.base_url}/api/upload/from-s3"

        try:
            self.logger.debug(f"Uploading page from S3 link for book {book_id}")

            # Send JSON payload (NOT form-data)
            payload = {
                "bookId": book_id,
                "imageUrls": [s3_url]  # Single-item array
            }

            response = requests.post(
                url,
                json=payload,  # IMPORTANT: json parameter, not data or files
                timeout=120
            )

            if response.status_code == 200:
                data = response.json()
                if data.get('success') and data.get('pages'):
                    page_id = data['pages'][0].get('id')
                    page_number = data['pages'][0].get('page_number')
                    self.logger.debug(f"Successfully uploaded page from S3 (page_number={page_number}), ID: {page_id}")
                    return page_id
                else:
                    self.logger.error(f"Upload succeeded but unexpected response format: {data}")
                    return None
            else:
                self.logger.error(f"Failed to upload page from S3. Status: {response.status_code}, Response: {response.text}")
                return None

        except requests.exceptions.RequestException as e:
            self.logger.error(f"Error uploading page from S3: {e}")
            return None
        except Exception as e:
            self.logger.error(f"Unexpected error uploading page from S3: {e}")
            return None

    def update_book_after_upload(self, book_id: str) -> bool:
        """
        Notify API that all pages for a book have been uploaded.
        Called after all pages are successfully uploaded.

        Args:
            book_id: Book ID from create_book response

        Returns:
            True if successful, False otherwise
        """
        url = f"{self.base_url}/api/upload/update-book"

        try:
            self.logger.debug(f"Updating book after upload completion: {book_id}")

            # Send JSON payload
            payload = {
                "bookId": book_id
            }

            response = requests.post(
                url,
                json=payload,
                timeout=30
            )

            if response.status_code == 200:
                self.logger.info(f"Successfully updated book {book_id} after upload")
                return True
            else:
                self.logger.error(f"Failed to update book after upload. Status: {response.status_code}, Response: {response.text}")
                return False

        except requests.exceptions.RequestException as e:
            self.logger.error(f"Error updating book after upload: {e}")
            return False
        except Exception as e:
            self.logger.error(f"Unexpected error updating book after upload: {e}")
            return False

    def verify_book(self, book_id: str) -> Optional[Dict]:
        """
        Verify book exists and get its details.
        Used for data integrity verification after migration.

        Args:
            book_id: Book ID to verify

        Returns:
            Book details dictionary or None if not found
        """
        url = f"{self.base_url}/api/books/{book_id}"

        try:
            response = requests.get(url, timeout=30)

            if response.status_code == 200:
                return response.json()
            else:
                self.logger.warning(f"Book {book_id} verification failed. Status: {response.status_code}")
                return None

        except requests.exceptions.RequestException as e:
            self.logger.error(f"Error verifying book {book_id}: {e}")
            return None