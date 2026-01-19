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
        self.book_create_endpoint = config.book_create_endpoint
        self.page_upload_endpoint = config.page_upload_endpoint
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
        url = f"{self.base_url}{self.book_create_endpoint}"

        # Map metadata fields to API expected format
        payload = {
            "title": metadata.get('Title') or 'Untitled',
            "author": metadata.get('Author') or 'Unknown',
            "language": metadata.get('Language') or 'Unknown',
            "published": metadata.get('Year of publication') or 'Unknown',
            "publisher": metadata.get('Publisher'),
            "place_of_publication": metadata.get('Place of publication'),
            "printer": metadata.get('Printer'),            
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

    def upload_page(self, book_id: str, page_file_path: str) -> Optional[str]:
        """
        Upload a page image to a book.
        Backend automatically determines page number based on upload order.

        Expected API response format:
        {
          "success": true,
          "uploaded": 1,
          "pages": [
            {
              "id": "page_id",
              "book_id": "book_id",
              "page_number": 1,  # Auto-assigned by backend
              ...other fields...
            }
          ]
        }

        Args:
            book_id: Book ID from create_book response
            page_file_path: Path to page image file

        Returns:
            Page ID from API response, or None if failed
        """
        # API endpoint is /api/upload (not book-specific)
        url = f"{self.base_url}/api/upload"

        try:
            self.logger.debug(f"Uploading page for book {book_id} from {page_file_path}")

            # Open file in binary mode
            with open(page_file_path, 'rb') as f:
                files = {
                    'file': (f.name, f, 'application/octet-stream')
                }

                # Include book_id in form data
                data = {
                    'book_id': book_id
                }

                response = requests.post(
                    url,
                    files=files,
                    data=data,
                    timeout=120  # Longer timeout for file upload
                )

            if response.status_code == 200:
                data = response.json()
                if data.get('success') and data.get('pages'):
                    page_id = data['pages'][0].get('id')
                    page_number = data['pages'][0].get('page_number')
                    self.logger.debug(f"Successfully uploaded page (page_number={page_number}), ID: {page_id}")
                    return page_id
                else:
                    self.logger.error(f"Upload succeeded but unexpected response format: {data}")
                    return None
            else:
                self.logger.error(f"Failed to upload page. Status: {response.status_code}, Response: {response.text}")
                return None

        except FileNotFoundError:
            self.logger.error(f"Page file not found: {page_file_path}")
            return None
        except requests.exceptions.RequestException as e:
            self.logger.error(f"Error uploading page: {e}")
            return None
        except Exception as e:
            self.logger.error(f"Unexpected error uploading page: {e}")
            return None

    def verify_book(self, book_id: str) -> Optional[Dict]:
        """
        Verify book exists and get its details.
        Used for data integrity verification after migration.

        Args:
            book_id: Book ID to verify

        Returns:
            Book details dictionary or None if not found
        """
        url = f"{self.base_url}{self.book_create_endpoint}/{book_id}"

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