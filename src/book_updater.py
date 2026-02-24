"""Book updater module for manually triggering update_book_after_upload API calls."""

import asyncio
import json
import logging
from typing import Dict, List, Optional, Tuple

from src.api_client import APIClient
from src.state_manager import StateManager


class BookUpdater:
    """Manually triggers update_book_after_upload for eligible books."""

    def __init__(self, api_client: APIClient, state_manager: StateManager, logger: logging.Logger):
        """
        Initialize BookUpdater.

        Args:
            api_client: APIClient instance for making API calls
            state_manager: StateManager instance for database queries
            logger: Logger instance for logging
        """
        self.api_client = api_client
        self.state_manager = state_manager
        self.logger = logger

    def _is_book_eligible(self, book: Dict) -> Tuple[bool, str]:
        """
        Check if a single book qualifies for update.

        Args:
            book: Book record dict from database

        Returns:
            Tuple of (is_eligible, reason)
        """
        barcode = book['picturae_barcode']
        api_book_id = book.get('api_book_id')
        status = book['migration_status']

        # Check if api_book_id exists
        if not api_book_id:
            return (False, "No API book ID")

        # For completed books, always eligible
        if status == 'completed':
            return (True, "Completed book")

        # For in_progress books, check for pending pages
        if status == 'in_progress':
            pending_pages = self.state_manager.get_pages_for_book(barcode, status='pending')
            if pending_pages:
                return (False, f"Has {len(pending_pages)} pending pages")
            else:
                return (True, "In-progress with all pages uploaded")

        # Other statuses not eligible
        return (False, f"Ineligible status: {status}")

    def _get_eligible_books(
        self,
        include_completed: bool,
        include_in_progress: bool,
        barcodes: Optional[List[str]]
    ) -> List[Dict]:
        """
        Query database and filter for eligible books.

        Args:
            include_completed: Include books with status 'completed'
            include_in_progress: Include books with status 'in_progress'
            barcodes: Optional list of specific barcodes to filter

        Returns:
            List of eligible book records
        """
        eligible_books = []

        # Build status list
        statuses = []
        if include_completed:
            statuses.append('completed')
        if include_in_progress:
            statuses.append('in_progress')

        if not statuses:
            self.logger.warning("No statuses selected for update")
            return []

        # Query books
        if barcodes:
            # Query specific barcodes
            self.logger.info(f"Querying {len(barcodes)} specific barcodes")
            books = []
            for barcode in barcodes:
                book = self.state_manager.get_book(barcode)
                if book:
                    # Check if book status matches our criteria
                    if book['migration_status'] in statuses:
                        books.append(book)
                    else:
                        self.logger.warning(
                            f"Book {barcode} has status '{book['migration_status']}', "
                            f"not in requested statuses {statuses}"
                        )
                else:
                    self.logger.error(f"Book not found: {barcode}")
        else:
            # Query all books by status
            self.logger.info(f"Querying books with statuses: {statuses}")
            books = self.state_manager.get_books_by_status(statuses)
            self.logger.info(f"Found {len(books)} books with matching statuses")

        # Filter through eligibility check
        for book in books:
            is_eligible, reason = self._is_book_eligible(book)
            if is_eligible:
                eligible_books.append(book)
                self.logger.debug(
                    f"Book {book['picturae_barcode']} eligible: {reason}"
                )
            else:
                self.logger.info(
                    f"Skipping book {book['picturae_barcode']}: {reason}"
                )

        self.logger.info(f"Total eligible books: {len(eligible_books)}")
        return eligible_books

    async def _update_single_book(
        self,
        book: Dict,
        dry_run: bool,
        max_retries: int
    ) -> Dict:
        """
        Update a single book (handles dry-run mode).

        Args:
            book: Book record dict
            dry_run: If True, preview without making API calls
            max_retries: Maximum retry attempts

        Returns:
            Result dict with keys: barcode, api_book_id, success, error
        """
        barcode = book['picturae_barcode']
        api_book_id = book['api_book_id']

        # Extract title from metadata
        try:
            metadata = json.loads(book['metadata_json'])
            title = metadata.get('Title', 'Untitled')
        except (json.JSONDecodeError, KeyError):
            title = 'Untitled'

        # Dry run mode
        if dry_run:
            self.logger.info(
                f"DRY RUN: Would update book {barcode} "
                f"(API ID: {api_book_id}, Title: {title})"
            )
            return {
                'barcode': barcode,
                'api_book_id': api_book_id,
                'success': True,
                'error': None
            }

        # Execute actual update
        try:
            self.logger.info(
                f"Updating book {barcode} (API ID: {api_book_id}, Title: {title})"
            )
            success = await self.api_client.update_book_after_upload(
                api_book_id,
                max_retries=max_retries
            )

            if success:
                return {
                    'barcode': barcode,
                    'api_book_id': api_book_id,
                    'success': True,
                    'error': None
                }
            else:
                error_msg = "API call returned False (all retries exhausted)"
                self.logger.error(f"Failed to update book {barcode}: {error_msg}")
                return {
                    'barcode': barcode,
                    'api_book_id': api_book_id,
                    'success': False,
                    'error': error_msg
                }

        except Exception as e:
            error_msg = str(e)
            self.logger.error(f"Exception updating book {barcode}: {error_msg}", exc_info=True)
            return {
                'barcode': barcode,
                'api_book_id': api_book_id,
                'success': False,
                'error': error_msg
            }

    async def update_eligible_books(
        self,
        include_completed: bool = True,
        include_in_progress: bool = True,
        barcodes: Optional[List[str]] = None,
        dry_run: bool = False,
        max_retries: int = 3,
        limit: Optional[int] = None
    ) -> Dict:
        """
        Update eligible books by calling update_book_after_upload.

        Args:
            include_completed: Include books with status 'completed'
            include_in_progress: Include books with status 'in_progress'
            barcodes: Optional list of specific barcodes to update
            dry_run: If True, preview without making API calls
            max_retries: Maximum retry attempts per book
            limit: Optional limit to first N eligible books

        Returns:
            Summary dict with keys: total, successful, failed, skipped, details
        """
        # Log operation start
        self.logger.info("=" * 80)
        self.logger.info("Starting update-books operation")
        self.logger.info(f"Parameters: include_completed={include_completed}, "
                        f"include_in_progress={include_in_progress}, "
                        f"dry_run={dry_run}, max_retries={max_retries}")
        if barcodes:
            self.logger.info(f"Target barcodes: {', '.join(barcodes)}")
        self.logger.info("=" * 80)

        # Get eligible books
        eligible_books = self._get_eligible_books(
            include_completed,
            include_in_progress,
            barcodes
        )

        # Check if any books found
        if not eligible_books:
            self.logger.warning("No eligible books found")
            return {
                'total': 0,
                'successful': 0,
                'failed': 0,
                'skipped': 0,
                'details': []
            }

        # Apply limit if specified
        if limit and limit < len(eligible_books):
            self.logger.info(f"Limiting to first {limit} books (out of {len(eligible_books)} eligible)")
            eligible_books = eligible_books[:limit]

        # Dry run mode announcement
        if dry_run:
            self.logger.info("=" * 80)
            self.logger.info("DRY RUN MODE - No API calls will be made")
            self.logger.info("=" * 80)

        # Initialize results
        results = {
            'total': len(eligible_books),
            'successful': 0,
            'failed': 0,
            'skipped': 0,
            'details': []
        }

        # Process each book
        self.logger.info(f"Processing {len(eligible_books)} eligible books...")
        for i, book in enumerate(eligible_books, 1):
            barcode = book['picturae_barcode']
            self.logger.info(f"[{i}/{len(eligible_books)}] Processing book {barcode}")

            result = await self._update_single_book(book, dry_run, max_retries)

            if result['success']:
                results['successful'] += 1
            else:
                results['failed'] += 1
                results['details'].append({
                    'barcode': result['barcode'],
                    'api_book_id': result['api_book_id'],
                    'error': result['error']
                })

        # Log summary
        self.logger.info("=" * 80)
        self.logger.info("Update operation completed")
        self.logger.info(
            f"Results: {results['successful']}/{results['total']} successful, "
            f"{results['failed']} failed"
        )
        if results['failed'] > 0:
            self.logger.error("Failed books:")
            for detail in results['details']:
                self.logger.error(
                    f"  - {detail['barcode']} (API ID: {detail['api_book_id']}): "
                    f"{detail['error']}"
                )
        self.logger.info("=" * 80)

        return results
