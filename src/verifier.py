"""Data integrity verification after migration."""

import logging
from typing import Dict, List, Optional

from src.api_client import APIClient
from src.state_manager import StateManager


class MigrationVerifier:
    """Verifies data integrity after migration by comparing local state with API."""

    def __init__(self, api_client: APIClient, state_manager: StateManager, logger: logging.Logger):
        """
        Initialize MigrationVerifier.

        Args:
            api_client: API client instance
            state_manager: State manager instance
            logger: Logger instance
        """
        self.api_client = api_client
        self.state_manager = state_manager
        self.logger = logger

    def verify_all_books(self, limit: Optional[int] = None) -> Dict:
        """
        Verify all completed books against API.

        Args:
            limit: Limit verification to N books (for testing)

        Returns:
            Dictionary with verification results
        """
        self.logger.info("=" * 60)
        self.logger.info("Starting Data Integrity Verification")
        self.logger.info("=" * 60)

        # Get completed books from database
        completed_books = self.state_manager.get_books_by_status('completed')

        if limit:
            completed_books = completed_books[:limit]

        self.logger.info(f"Verifying {len(completed_books)} completed books...")

        results = {
            'total_books': len(completed_books),
            'verified_books': 0,
            'failed_books': 0,
            'missing_books': 0,
            'page_mismatches': 0,
            'details': []
        }

        for book in completed_books:
            barcode = book['picturae_barcode']
            api_book_id = book['api_book_id']

            try:
                verification = self._verify_single_book(barcode, api_book_id)

                if verification['status'] == 'verified':
                    results['verified_books'] += 1
                elif verification['status'] == 'missing':
                    results['missing_books'] += 1
                    results['details'].append(verification)
                elif verification['status'] == 'page_mismatch':
                    results['page_mismatches'] += 1
                    results['details'].append(verification)
                else:
                    results['failed_books'] += 1
                    results['details'].append(verification)

            except Exception as e:
                self.logger.error(f"Error verifying book {barcode}: {e}")
                results['failed_books'] += 1
                results['details'].append({
                    'barcode': barcode,
                    'status': 'error',
                    'error': str(e)
                })

        # Print summary
        self._print_verification_summary(results)

        return results

    def _verify_single_book(self, barcode: str, api_book_id: str) -> Dict:
        """
        Verify a single book and its pages.

        Args:
            barcode: Picturae barcode
            api_book_id: API book ID

        Returns:
            Dictionary with verification details
        """
        self.logger.debug(f"Verifying book {barcode} (API ID: {api_book_id})")

        # Get book from API (with pages)
        api_url = f"{self.api_client.base_url}/api/books/{api_book_id}"

        try:
            import requests
            response = requests.get(api_url, timeout=30)

            if response.status_code == 404:
                self.logger.warning(f"Book {barcode} not found in API (ID: {api_book_id})")
                return {
                    'barcode': barcode,
                    'api_book_id': api_book_id,
                    'status': 'missing',
                    'error': 'Book not found in API'
                }

            if response.status_code != 200:
                self.logger.error(f"API error for book {barcode}: {response.status_code}")
                return {
                    'barcode': barcode,
                    'api_book_id': api_book_id,
                    'status': 'api_error',
                    'error': f"HTTP {response.status_code}"
                }

            api_data = response.json()

        except Exception as e:
            self.logger.error(f"Error fetching book {barcode} from API: {e}")
            return {
                'barcode': barcode,
                'api_book_id': api_book_id,
                'status': 'error',
                'error': str(e)
            }

        # Get pages from database
        db_pages = self.state_manager.get_pages_for_book(barcode, status='uploaded')
        db_page_count = len(db_pages)

        # Get pages from API response
        api_pages = api_data.get('pages', [])
        api_page_count = len(api_pages)

        # Verify page count matches
        if db_page_count != api_page_count:
            self.logger.warning(
                f"Page count mismatch for book {barcode}: "
                f"DB={db_page_count}, API={api_page_count}"
            )
            return {
                'barcode': barcode,
                'api_book_id': api_book_id,
                'status': 'page_mismatch',
                'db_page_count': db_page_count,
                'api_page_count': api_page_count,
                'expected_page_count': db_page_count,
                'error': f'Expected {db_page_count} pages, found {api_page_count}'
            }

        # Verify each page ID exists in API response
        db_page_ids = {page['api_page_id'] for page in db_pages if page.get('api_page_id')}
        api_page_ids = {page['id'] for page in api_pages}

        missing_in_api = db_page_ids - api_page_ids
        extra_in_api = api_page_ids - db_page_ids

        if missing_in_api or extra_in_api:
            self.logger.warning(
                f"Page ID mismatch for book {barcode}: "
                f"Missing={len(missing_in_api)}, Extra={len(extra_in_api)}"
            )
            return {
                'barcode': barcode,
                'api_book_id': api_book_id,
                'status': 'page_mismatch',
                'db_page_count': db_page_count,
                'api_page_count': api_page_count,
                'missing_page_ids': list(missing_in_api),
                'extra_page_ids': list(extra_in_api),
                'error': f'Page IDs do not match'
            }

        # All checks passed
        self.logger.debug(f"Book {barcode} verified successfully ({db_page_count} pages)")
        return {
            'barcode': barcode,
            'api_book_id': api_book_id,
            'status': 'verified',
            'page_count': db_page_count
        }

    def _print_verification_summary(self, results: Dict) -> None:
        """Print verification summary."""
        print("\n" + "=" * 60)
        print("Verification Results")
        print("=" * 60)
        print(f"Total Books Checked:     {results['total_books']}")
        print(f"✓ Verified Successfully: {results['verified_books']}")
        print(f"✗ Missing in API:        {results['missing_books']}")
        print(f"⚠ Page Mismatches:       {results['page_mismatches']}")
        print(f"✗ Verification Errors:   {results['failed_books']}")

        if results['verified_books'] == results['total_books']:
            print("\n🎉 All books verified successfully!")
        else:
            print(f"\n⚠️  {results['total_books'] - results['verified_books']} books have issues")

        # Show details for failed verifications
        if results['details']:
            print("\nDetailed Issues:")
            for detail in results['details'][:10]:  # Show first 10 issues
                print(f"\n  Book: {detail['barcode']}")
                print(f"  Status: {detail['status']}")
                if 'error' in detail:
                    print(f"  Error: {detail['error']}")
                if 'db_page_count' in detail:
                    print(f"  DB Pages: {detail['db_page_count']}, API Pages: {detail['api_page_count']}")

            if len(results['details']) > 10:
                print(f"\n  ... and {len(results['details']) - 10} more issues")

        print("=" * 60)

    def export_verification_report(self, results: Dict, output_file: str) -> None:
        """
        Export verification results to a CSV file.

        Args:
            results: Verification results dictionary
            output_file: Path to output CSV file
        """
        import csv

        with open(output_file, 'w', newline='') as f:
            writer = csv.writer(f)
            writer.writerow(['Barcode', 'API Book ID', 'Status', 'DB Pages', 'API Pages', 'Error'])

            for detail in results['details']:
                writer.writerow([
                    detail.get('barcode', ''),
                    detail.get('api_book_id', ''),
                    detail.get('status', ''),
                    detail.get('db_page_count', ''),
                    detail.get('api_page_count', ''),
                    detail.get('error', '')
                ])

        self.logger.info(f"Verification report exported to: {output_file}")

    def verify_book_metadata(self, barcode: str) -> Dict:
        """
        Verify book metadata matches between database and API.

        Args:
            barcode: Picturae barcode

        Returns:
            Dictionary comparing metadata fields
        """
        book = self.state_manager.get_book(barcode)
        if not book or not book.get('api_book_id'):
            return {'error': 'Book not found or not migrated'}

        import json
        db_metadata = json.loads(book['metadata_json'])

        # Get from API
        api_url = f"{self.api_client.base_url}/api/books/{book['api_book_id']}"
        try:
            import requests
            response = requests.get(api_url, timeout=30)
            if response.status_code == 200:
                api_data = response.json()

                comparison = {
                    'barcode': barcode,
                    'title': {
                        'db': db_metadata.get('Title'),
                        'api': api_data.get('title'),
                        'match': db_metadata.get('Title') == api_data.get('title')
                    },
                    'author': {
                        'db': db_metadata.get('Author'),
                        'api': api_data.get('author'),
                        'match': db_metadata.get('Author') == api_data.get('author')
                    },
                    'language': {
                        'db': db_metadata.get('Language'),
                        'api': api_data.get('language'),
                        'match': db_metadata.get('Language') == api_data.get('language')
                    }
                }

                return comparison
            else:
                return {'error': f'API returned status {response.status_code}'}

        except Exception as e:
            return {'error': str(e)}
