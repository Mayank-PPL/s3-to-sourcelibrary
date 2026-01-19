"""Migration orchestrator coordinating the entire migration workflow."""

import logging
import signal
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

from tqdm import tqdm

from src.api_client import APIClient
from src.csv_handler import CSVHandler
from src.s3_locator import S3Locator
from src.state_manager import StateManager
from src.utils import cleanup_temp_file, format_bytes


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
            pages_count = 0
            total_size = 0

            for page_data in self.csv_handler.parse_pages_csv(self.config.pages_csv_path):
                barcode = page_data['barcode']
                filename = page_data['filename']
                dam_directory = page_data['dam_directory']
                sequence_order = page_data['sequence_order']

                # Construct S3 key and check if file exists
                s3_key, file_size = self.s3_locator.verify_and_get_file_info(dam_directory, filename)

                if s3_key:
                    # File found
                    self.state_manager.insert_page(
                        barcode, filename, dam_directory, s3_key, sequence_order, file_size
                    )
                    total_size += file_size
                else:
                    # File not found - log as missing
                    self.state_manager.add_missing_file(
                        barcode, filename, dam_directory, "File not found in S3"
                    )
                    self.logger.warning(f"Missing file: {dam_directory}/{filename}")

                pages_count += 1

                if pages_count % 10000 == 0:
                    self.logger.info(f"Indexed {pages_count} pages...")

            self.logger.info(f"Indexed {pages_count} pages total")
            self.logger.info(f"Total size: {format_bytes(total_size)}")

            # Update book total_pages counts
            self.logger.info("Updating book page counts...")
            for barcode, count in page_counts.items():
                book = self.state_manager.get_book(barcode)
                if book:
                    self.state_manager.insert_book(barcode, eval(book['metadata_json']), total_pages=count)

            # Mark indexing as completed
            self.state_manager.mark_indexing_completed()
            self.state_manager.set_metadata("total_books_indexed", str(books_count))
            self.state_manager.set_metadata("total_pages_indexed", str(pages_count))
            self.state_manager.set_metadata("total_size_bytes", str(total_size))

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

        # Get books to migrate (pending or failed)
        books = self.state_manager.get_all_books(limit=limit)

        # Filter to only pending books
        pending_books = [b for b in books if b['migration_status'] in ['pending', 'in_progress', 'failed']]

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

        Args:
            book: Book record from database
        """
        barcode = book['picturae_barcode']
        self.logger.info(f"Processing book: {barcode}")

        try:
            # Check if book already created
            api_book_id = book.get('api_book_id')

            if not api_book_id:
                # Create book via API
                import json
                metadata = json.loads(book['metadata_json'])
                api_book_id = self.api_client.create_book(metadata)

                if not api_book_id:
                    raise Exception("Failed to create book via API")

                # Update database with API book ID
                self.state_manager.update_book_api_id(barcode, api_book_id)

            # Get pending pages for this book (ordered by sequence)
            pages = self.state_manager.get_pages_for_book(barcode, status='pending')

            if not pages:
                self.logger.info(f"No pending pages for book {barcode}, marking as completed")
                self.state_manager.update_book_status(barcode, 'completed')
                return

            self.logger.info(f"Uploading {len(pages)} pages for book {barcode} (sequential)")

            # Upload pages ONE AT A TIME in sequence order
            for page in pages:
                if self.shutdown_requested:
                    break

                self._upload_single_page(barcode, api_book_id, page)

            # Mark book as completed if all pages uploaded
            remaining_pages = self.state_manager.get_pages_for_book(barcode, status='pending')
            if not remaining_pages:
                self.state_manager.update_book_status(barcode, 'completed')
                self.logger.info(f"Book {barcode} migration completed")
            else:
                self.logger.warning(f"Book {barcode} has {len(remaining_pages)} pending pages remaining")

        except Exception as e:
            self.logger.error(f"Error migrating book {barcode}: {e}", exc_info=True)
            self.state_manager.update_book_status(barcode, 'failed', str(e))

    def _upload_single_page(self, barcode: str, api_book_id: str, page: dict) -> None:
        """
        Upload a single page: download from S3, upload to API, delete temp file.

        Args:
            barcode: Book barcode
            api_book_id: API book ID
            page: Page record from database
        """
        page_id = page['id']
        s3_key = page['s3_key']
        filename = page['filename']

        temp_file = None

        try:
            # Step 1: Download from S3
            temp_file = self.s3_locator.download_file(s3_key, barcode, filename)

            if not temp_file:
                raise Exception(f"Failed to download page from S3: {s3_key}")

            # Step 2: Upload to API
            api_page_id = self.api_client.upload_page(api_book_id, temp_file)

            if not api_page_id:
                raise Exception("Failed to upload page to API")

            # Step 3: Update database
            self.state_manager.update_page_uploaded(page_id, api_page_id)
            self.state_manager.increment_uploaded_pages(barcode)

        except Exception as e:
            self.logger.error(f"Error uploading page {page_id}: {e}")
            self.state_manager.update_page_failed(page_id, str(e))

        finally:
            # Step 4: Cleanup temp file
            if temp_file:
                cleanup_temp_file(temp_file, self.logger)

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
