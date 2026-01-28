"""Migration orchestrator coordinating the entire migration workflow."""

import asyncio
import json
import logging
import signal
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

from tqdm import tqdm

from src.api_client import APIClient
from src.csv_handler import CSVHandler
from src.s3_locator import S3Locator
from src.state_manager import StateManager
from src.utils import format_bytes


class MigrationOrchestrator:
    """Coordinates the migration from S3 to Source Library."""

    def __init__(self, config, logger: logging.Logger):
        """
        Initialize MigrationOrchestrator.

        Args:
            config: Configuration object
            logger: Logger instance
        """
        self.config = config
        self.logger = logger
        self.shutdown_requested = False

        # Initialize components
        self.state_manager = StateManager(config.state_db_path)
        self.csv_handler = CSVHandler(logger)
        self.s3_locator = S3Locator(config, logger)
        self.api_client = APIClient(config, logger)

        # Setup signal handlers for graceful shutdown
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

    def _signal_handler(self, signum, frame):
        """Handle shutdown signals gracefully."""
        self.logger.info("Shutdown signal received. Finishing current operations...")
        self.shutdown_requested = True

    def run_indexing(self, force: bool = False) -> bool:
        """
        Run indexing phase: parse CSVs and populate database.

        Args:
            force: Force re-indexing even if already indexed

        Returns:
            True if successful, False otherwise
        """
        if self.state_manager.is_indexed() and not force:
            self.logger.info("Database already indexed. Use --force to re-index.")
            return True

        if force:
            self.logger.info("Force re-indexing: clearing existing data...")
            self.state_manager.delete_all_data()

        self.logger.info("=" * 60)
        self.logger.info("Starting Indexing Phase")
        self.logger.info("=" * 60)

        try:
            # Step 1: Parse and index books
            self.logger.info("Step 1/3: Indexing books from CSV...")
            books_count = 0

            for book_data in self.csv_handler.parse_books_csv(self.config.books_csv_path):
                barcode = book_data['barcode']
                metadata = book_data['metadata']

                self.state_manager.insert_book(barcode, metadata, total_pages=0)
                books_count += 1

                if books_count % 100 == 0:
                    self.logger.info(f"Indexed {books_count} books...")

            self.logger.info(f"Indexed {books_count} books total")

            # Step 2: Count pages per book from Pages CSV
            self.logger.info("Step 2/3: Counting pages per book...")
            page_counts = self.csv_handler.count_pages_by_book(self.config.pages_csv_path)

            # Step 3: Parse and index pages
            self.logger.info("Step 3/3: Indexing pages from CSV...")
            self.logger.info("Note: S3 verification skipped during indexing for speed (will verify during migration)")
            pages_count = 0

            for page_data in self.csv_handler.parse_pages_csv(self.config.pages_csv_path):
                barcode = page_data['barcode']
                filename = page_data['filename']
                dam_directory = page_data['dam_directory']
                sequence_order = page_data['sequence_order']

                # Construct expected S3 key WITHOUT verifying (no S3 API calls during indexing)
                # Assume .jp2 extension; will try alternatives during migration if needed
                s3_key = self.s3_locator.construct_s3_key(dam_directory, filename, '.jp2')

                # Store with unknown file size (will be determined during migration)
                self.state_manager.insert_page(
                    barcode, filename, dam_directory, s3_key, sequence_order, file_size_bytes=0
                )

                pages_count += 1

                if pages_count % 10000 == 0:
                    self.logger.info(f"Indexed {pages_count} pages...")

            self.logger.info(f"Indexed {pages_count} pages total")
            self.logger.info("File sizes and existence will be verified during migration phase")

            # Update book total_pages counts
            self.logger.info("Updating book page counts...")
            for barcode, count in page_counts.items():
                book = self.state_manager.get_book(barcode)
                if book:
                    self.state_manager.insert_book(barcode, json.loads(book['metadata_json']), total_pages=count)

            # Mark indexing as completed
            self.state_manager.mark_indexing_completed()
            self.state_manager.set_metadata("total_books_indexed", str(books_count))
            self.state_manager.set_metadata("total_pages_indexed", str(pages_count))
            # Size will be determined during migration (set to 0 for now)
            self.state_manager.set_metadata("total_size_bytes", "0")

            missing_count = self.state_manager.get_missing_files_count()
            if missing_count > 0:
                self.logger.warning(f"Found {missing_count} missing files (logged in missing_files table)")

            self.logger.info("=" * 60)
            self.logger.info("Indexing Phase Completed Successfully")
            self.logger.info("=" * 60)

            return True

        except Exception as e:
            self.logger.error(f"Indexing failed: {e}", exc_info=True)
            return False

    def run_migration(self, limit: Optional[int] = None) -> bool:
        """
        Run migration phase: create books and upload pages.

        Args:
            limit: Limit migration to N books (for testing)

        Returns:
            True if successful, False otherwise
        """
        if not self.state_manager.is_indexed():
            self.logger.error("Database not indexed. Run indexing first.")
            return False

        self.logger.info("=" * 60)
        self.logger.info("Starting Migration Phase")
        self.logger.info("=" * 60)

        # Get books to migrate (pending or in_progress only)
        pending_books = self.state_manager.get_books_by_status(
            statuses=['pending', 'in_progress'],
            limit=limit
        )

        if not pending_books:
            self.logger.info("No books to migrate. All books completed.")
            return True

        self.logger.info(f"Migrating {len(pending_books)} books (book-level concurrency: {self.config.book_workers})")

        # Use ThreadPoolExecutor for book-level concurrency
        with ThreadPoolExecutor(max_workers=self.config.book_workers) as executor:
            futures = []

            for book in pending_books:
                if self.shutdown_requested:
                    break

                future = executor.submit(self._migrate_single_book, book)
                futures.append(future)

            # Wait for all books to complete with progress bar
            for future in tqdm(as_completed(futures), total=len(futures), desc="Books"):
                if self.shutdown_requested:
                    self.logger.info("Shutdown requested, waiting for in-progress books to finish...")
                    break

                try:
                    future.result()
                except Exception as e:
                    self.logger.error(f"Book migration failed: {e}", exc_info=True)

        # Show final statistics
        stats = self.state_manager.get_migration_statistics()
        self.logger.info("=" * 60)
        self.logger.info("Migration Phase Completed")
        self.logger.info(f"Books: {stats['completed_books']}/{stats['total_books']} completed")
        self.logger.info(f"Pages: {stats['uploaded_pages']}/{stats['total_pages']} uploaded")
        self.logger.info(f"Data: {format_bytes(stats['uploaded_bytes'])} / {format_bytes(stats['total_bytes'])}")
        self.logger.info("=" * 60)

        return stats['completed_books'] == len(pending_books)

    def _migrate_single_book(self, book: dict) -> None:
        """
        Migrate a single book: create book and upload all pages sequentially.

        This is a synchronous wrapper that runs async code.

        Args:
            book: Book record from database
        """
        # Run async code in a new event loop
        asyncio.run(self._migrate_single_book_async(book))

    async def _migrate_single_book_async(self, book: dict) -> None:
        """
        Migrate a single book: create book and upload all pages sequentially (async).

        Args:
            book: Book record from database
        """
        barcode = book['picturae_barcode']
        title = json.loads(book['metadata_json']).get('Title', 'Untitled')
        self.logger.info(f"Processing book: {barcode}, {title}")

        try:
            # Check if book already created
            api_book_id = book.get('api_book_id')

            if not api_book_id:
                # Create book via API
                metadata = json.loads(book['metadata_json'])
                api_book_id = await self.api_client.create_book(metadata, max_retries=self.config.max_retries)

                if not api_book_id:
                    raise Exception("Failed to create book via API")

                # Update database with API book ID
                self.state_manager.update_book_api_id(barcode, api_book_id)

            # Get pending pages for this book (to support retry on restart)
            pending_pages = self.state_manager.get_pages_for_book(barcode, status='pending')

            # Combine and sort by sequence_order to maintain correct ordering
            pages = pending_pages
            pages = sorted(pages, key=lambda p: p['sequence_order'])

            if not pages:
                self.logger.info(f"No pending/failed pages for book {barcode}, marking as completed")
                self.state_manager.update_book_status(barcode, 'completed')
                return

            self.logger.info(f"Uploading {len(pages)} pages for book {barcode} (sequential)")

            # Upload pages ONE AT A TIME in sequence order
            for idx, page in enumerate(pages):
                if self.shutdown_requested:
                    break

                await self._upload_single_page_async(barcode, api_book_id, page)

                # Add delay between requests to avoid rate limiting (skip delay after last page)
                if idx < len(pages) - 1 and self.config.request_delay > 0:
                    await asyncio.sleep(self.config.request_delay)

            # Mark book as completed if all pages uploaded
            # Check uploaded count against total pages (future-proof for any status types)
            uploaded_count = self.state_manager.count_uploaded_pages_for_book(barcode)
            total_pages = book['total_pages']

            if uploaded_count == total_pages:
                # All pages successfully uploaded
                update_success = await self.api_client.update_book_after_upload(api_book_id, max_retries=self.config.max_retries)
                if update_success:
                    self.state_manager.update_book_status(barcode, 'completed')
                    self.logger.info(f"Book {barcode} migration completed: {uploaded_count}/{total_pages} pages uploaded, API notified")
                else:
                    self.logger.warning(f"Book {barcode} pages uploaded but failed to notify API")
                    self.state_manager.update_book_status(barcode, 'completed')  # Still mark as completed
            else:
                remaining = total_pages - uploaded_count
                self.logger.warning(f"Book {barcode} incomplete: {uploaded_count}/{total_pages} pages uploaded, {remaining} remaining")

        except Exception as e:
            self.logger.error(f"Error migrating book {barcode}: {e}", exc_info=True)
            self.state_manager.update_book_status(barcode, 'failed', str(e))

    async def _upload_single_page_async(self, barcode: str, api_book_id: str, page: dict) -> None:
        """
        Upload a single page: validate S3 file, generate presigned URL, send to API (async).

        New flow (no temp files):
        1. Validate S3 file exists (with extension fallback)
        2. Generate presigned URL
        3. Send presigned URL to API via JSON payload
        4. Update database

        Args:
            barcode: Book barcode
            api_book_id: API book ID
            page: Page record from database
        """
        page_id = page['id']
        s3_key = page['s3_key']
        filename = page['filename']
        dam_directory = page['dam_directory']
        presigned_url = None

        try:
            # Step 1: Validate S3 file exists
            # Try exact key first (from indexing with .jp2 assumption)
            exists, file_size = await self.s3_locator.check_file_exists(s3_key)

            if not exists:
                # Fallback: try different extensions
                self.logger.debug(f"File not found at {s3_key}, trying alternative extensions...")
                s3_key_found, file_size, ext_found = await self.s3_locator.find_file_with_extension(
                    dam_directory, filename
                )

                if not s3_key_found:
                    raise Exception(f"S3 file not found: {s3_key}")

                s3_key = s3_key_found
                self.logger.debug(f"Found file with extension {ext_found}: {s3_key} ({file_size} bytes)")

            # Step 2: Generate presigned URL
            presigned_url = await self.s3_locator.generate_presigned_url(s3_key)

            if not presigned_url:
                raise Exception(f"Failed to generate presigned URL for: {s3_key}")

            # Step 3: Upload to API using S3 presigned URL
            api_page_id = await self.api_client.upload_page_from_s3(api_book_id, presigned_url, max_retries=self.config.max_retries)

            if not api_page_id:
                raise Exception("Failed to upload page to API via S3 link")

            # Step 4: Update database with success
            self.state_manager.update_page_uploaded(page_id, api_page_id)
            self.state_manager.increment_uploaded_pages(barcode)

            self.logger.info(f"✓ Uploaded page {filename} (no temp file needed)")

        except Exception as e:
            # Log S3 URL if available to help with debugging
            s3_info = f", S3 URL: {presigned_url}" if presigned_url else ""
            self.logger.error(f"Error uploading page {page_id} (barcode: {barcode}, filename: {filename}): {e}{s3_info}")
            self.state_manager.update_page_failed(page_id, str(e))

    def show_status(self) -> None:
        """Show current migration status and statistics."""
        stats = self.state_manager.get_migration_statistics()

        print("=" * 60)
        print("Migration Status")
        print("=" * 60)
        print(f"\nBooks:")
        print(f"  Total:       {stats['total_books']}")
        print(f"  Completed:   {stats['completed_books']}")
        print(f"  In Progress: {stats['in_progress_books']}")
        print(f"  Pending:     {stats['pending_books']}")
        print(f"  Failed:      {stats['failed_books']}")

        print(f"\nPages:")
        print(f"  Total:    {stats['total_pages']}")
        print(f"  Uploaded: {stats['uploaded_pages']}")
        print(f"  Pending:  {stats['pending_pages']}")
        print(f"  Failed:   {stats['failed_pages']}")

        print(f"\nData:")
        print(f"  Total:    {format_bytes(stats['total_bytes'])}")
        print(f"  Uploaded: {format_bytes(stats['uploaded_bytes'])}")

        if stats['total_bytes'] > 0:
            percent = (stats['uploaded_bytes'] / stats['total_bytes']) * 100
            print(f"  Progress: {percent:.1f}%")

        missing_count = self.state_manager.get_missing_files_count()
        if missing_count > 0:
            print(f"\nMissing Files: {missing_count}")

        print("=" * 60)
