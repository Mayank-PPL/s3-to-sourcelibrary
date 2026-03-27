"""Main CLI entry point for S3 to Source Library migration tool."""

import argparse
import sys

from src.config import load_config
from src.orchestrator import MigrationOrchestrator
from src.state_manager import StateManager
from src.utils import setup_logging


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="S3 to Source Library Migration Tool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Auto-mode: Index if needed, then migrate
  python -m src.main

  # Manual commands:
  python -m src.main index                    # Build index from CSVs
  python -m src.main verify-index             # Show index statistics
  python -m src.main migrate                  # Run migration
  python -m src.main migrate --limit 10       # Migrate first 10 books
  python -m src.main status                   # Show migration progress
  python -m src.main verify                   # Verify data integrity
  python -m src.main verify --export report.csv  # Export verification report
  python -m src.main reset --book RIT123      # Reset specific book
  python -m src.main reset --all-migration    # Reset all migration state
  python -m src.main update-books --dry-run   # Preview eligible books
  python -m src.main update-books             # Update all eligible books
  python -m src.main update-books --completed-only  # Update only completed books
  python -m src.main update-books --books RIT001,RIT002  # Update specific books
  python -m src.main reconcile --dry-run         # Preview which books would be reset
  python -m src.main reconcile                   # Reset DB entries for platform-deleted books
        """
    )

    subparsers = parser.add_subparsers(dest='command', help='Command to execute')

    # Index command
    index_parser = subparsers.add_parser('index', help='Build index from CSV files')
    index_parser.add_argument('--force', action='store_true', help='Force re-index even if already indexed')

    # Verify index command
    subparsers.add_parser('verify-index', help='Show index statistics and verify integrity')

    # Migrate command
    migrate_parser = subparsers.add_parser('migrate', help='Run migration using pre-built index')
    migrate_parser.add_argument('--limit', type=int, help='Limit migration to N books (for testing)')

    # Status command
    subparsers.add_parser('status', help='Show real-time migration progress')

    # Verify command
    verify_parser = subparsers.add_parser('verify', help='Verify data integrity after migration')
    verify_parser.add_argument('--limit', type=int, help='Limit verification to N books (for testing)')
    verify_parser.add_argument('--export', type=str, help='Export results to CSV file')

    # Reset command
    reset_parser = subparsers.add_parser('reset', help='Reset migration state')
    reset_parser.add_argument('--book', type=str, help='Reset specific book by Picturae barcode')
    reset_parser.add_argument('--all-migration', action='store_true', help='Reset all migration state (keeps index)')
    reset_parser.add_argument('--full', action='store_true', help='Delete entire database (full reset)')

    # Reconcile command
    reconcile_parser = subparsers.add_parser(
        'reconcile',
        help='Reconcile DB state with platform: reset books deleted from the platform'
    )
    reconcile_parser.add_argument(
        '--dry-run',
        action='store_true',
        help='Show what would be reset without making any changes'
    )
    reconcile_parser.add_argument(
        '--limit',
        type=int,
        help='Limit to first N books with an API ID (for testing)'
    )

    # Update-books command
    update_parser = subparsers.add_parser(
        'update-books',
        help='Manually trigger update_book_after_upload for eligible books'
    )
    update_parser.add_argument(
        '--dry-run',
        action='store_true',
        help='Preview which books would be updated without making API calls'
    )
    update_parser.add_argument(
        '--books',
        type=str,
        help='Comma-separated list of barcodes to update (e.g., RIT001000021,RIT001000022)'
    )
    update_parser.add_argument(
        '--completed-only',
        action='store_true',
        help='Only update books with status "completed"'
    )
    update_parser.add_argument(
        '--in-progress-only',
        action='store_true',
        help='Only update books with status "in_progress" (that have no pending pages)'
    )
    update_parser.add_argument(
        '--max-retries',
        type=int,
        default=3,
        help='Maximum retry attempts per book (default: 3)'
    )
    update_parser.add_argument(
        '--limit',
        type=int,
        help='Limit to first N eligible books (for testing)'
    )

    args = parser.parse_args()

    # Load configuration
    try:
        config = load_config()
    except Exception as e:
        print(f"Error loading configuration: {e}")
        print("Please ensure .env file exists and contains all required settings.")
        sys.exit(1)

    # Setup logging
    logger = setup_logging(config.log_file, config.log_level)

    # Create orchestrator
    orchestrator = MigrationOrchestrator(config, logger)

    # Handle commands
    if args.command == 'index':
        logger.info("Running index command...")
        success = orchestrator.run_indexing(force=args.force)
        sys.exit(0 if success else 1)

    elif args.command == 'verify-index':
        logger.info("Verifying index...")
        verify_index(orchestrator.state_manager)
        sys.exit(0)

    elif args.command == 'migrate':
        logger.info("Running migrate command...")
        success = orchestrator.run_migration(limit=args.limit)
        sys.exit(0 if success else 1)

    elif args.command == 'status':
        orchestrator.show_status()
        sys.exit(0)

    elif args.command == 'verify':
        logger.info("Running verification...")
        from src.verifier import MigrationVerifier
        verifier = MigrationVerifier(
            orchestrator.api_client,
            orchestrator.state_manager,
            logger
        )
        results = verifier.verify_all_books(limit=args.limit)

        if args.export:
            verifier.export_verification_report(results, args.export)

        # Exit with error code if any issues found
        has_issues = (results['missing_books'] + results['page_mismatches'] + results['failed_books']) > 0
        sys.exit(1 if has_issues else 0)

    elif args.command == 'reset':
        logger.info("Running reset command...")
        handle_reset(orchestrator.state_manager, args, logger)
        sys.exit(0)

    elif args.command == 'reconcile':
        logger.info("Running reconcile command...")
        handle_reconcile(orchestrator, config, args, logger)
        sys.exit(0)

    elif args.command == 'update-books':
        import asyncio
        from src.book_updater import BookUpdater

        logger.info("Running update-books command...")

        # Initialize BookUpdater
        updater = BookUpdater(
            orchestrator.api_client,
            orchestrator.state_manager,
            logger
        )

        # Parse barcode list if provided
        barcodes = None
        if args.books:
            barcodes = [b.strip() for b in args.books.split(',')]

        # Determine which statuses to include
        include_completed = not args.in_progress_only
        include_in_progress = not args.completed_only

        # Run async update
        results = asyncio.run(updater.update_eligible_books(
            include_completed=include_completed,
            include_in_progress=include_in_progress,
            barcodes=barcodes,
            dry_run=args.dry_run,
            max_retries=args.max_retries,
            limit=args.limit
        ))

        # Log final summary
        if results['failed'] > 0:
            logger.error(f"Update completed with {results['failed']} failures")
            sys.exit(1)
        else:
            logger.info("Update completed successfully")
            sys.exit(0)

    else:
        # No command specified - run in auto mode
        logger.info("Running in AUTO mode (index if needed + migrate)")

        # Check if indexed
        if not orchestrator.state_manager.is_indexed():
            logger.info("Database not indexed. Running indexing first...")
            success = orchestrator.run_indexing()
            if not success:
                logger.error("Indexing failed. Exiting.")
                sys.exit(1)

        # Run migration
        logger.info("Running migration...")
        success = orchestrator.run_migration()
        sys.exit(0 if success else 1)


def verify_index(state_manager: StateManager) -> None:
    """Verify index and show statistics."""
    if not state_manager.is_indexed():
        print("Database has not been indexed yet.")
        print("Run: python -m src.main index")
        return

    # Get metadata
    total_books = state_manager.get_metadata("total_books_indexed")
    total_pages = state_manager.get_metadata("total_pages_indexed")
    total_size = state_manager.get_metadata("total_size_bytes")
    indexed_at = state_manager.get_metadata("indexing_completed_at")

    print("=" * 60)
    print("Index Verification")
    print("=" * 60)
    print(f"Indexed at:   {indexed_at}")
    print(f"Total books:  {total_books}")
    print(f"Total pages:  {total_pages}")

    if total_size:
        from src.utils import format_bytes
        print(f"Total size:   {format_bytes(int(total_size))}")

    # Check for missing files
    missing_count = state_manager.get_missing_files_count()
    if missing_count > 0:
        print(f"\nWarning: {missing_count} files not found in S3")
        print("Query missing_files table for details:")
        print(f"  sqlite3 {state_manager.db_path}")
        print("  SELECT * FROM missing_files LIMIT 10;")

    # Sample some books
    print("\nSample books:")
    books = state_manager.get_all_books(limit=5)
    for book in books:
        barcode = book['picturae_barcode']
        total_pages = book['total_pages']
        print(f"  {barcode}: {total_pages} pages")

    print("=" * 60)


def handle_reset(state_manager: StateManager, args, logger) -> None:
    """Handle reset commands."""
    if args.full:
        confirm = input("WARNING: This will delete ALL data including index. Continue? (yes/no): ")
        if confirm.lower() == 'yes':
            state_manager.delete_all_data()
            logger.info("Full reset completed. All data deleted.")
        else:
            logger.info("Reset cancelled.")

    elif args.all_migration:
        confirm = input("WARNING: This will reset ALL migration state (index will be kept). Continue? (yes/no): ")
        if confirm.lower() == 'yes':
            state_manager.reset_all_migration_state()
            logger.info("Migration state reset completed. Index preserved.")
        else:
            logger.info("Reset cancelled.")

    elif args.book:
        barcode = args.book
        book = state_manager.get_book(barcode)
        if not book:
            logger.error(f"Book not found: {barcode}")
            return

        state_manager.reset_book_migration(barcode)
        logger.info(f"Reset migration state for book: {barcode}")

    else:
        logger.error("No reset option specified. Use --book, --all-migration, or --full")


def handle_reconcile(orchestrator, config, args, logger) -> None:
    """
    Reconcile DB state with the platform.

    For every book in the DB that has an api_book_id, check whether it still
    exists on the platform.  Books that return HTTP 404 were deleted from the
    platform; reset them to 'pending' so the next migration run re-creates
    them and uploads their pages.

    Books that return HTTP 200 (still exist) are left untouched — the
    migration tool will continue uploading their pending pages on the next run.

    Books that return any other status (network error, 5xx, etc.) are skipped
    rather than reset, so we don't accidentally wipe legitimate state due to a
    transient failure.
    """
    import asyncio

    state_manager = orchestrator.state_manager
    books = state_manager.get_books_with_api_id()
    if args.limit:
        books = books[:args.limit]

    if not books:
        logger.info("No books with an API ID found in DB — nothing to reconcile.")
        return

    results = asyncio.run(_reconcile_async(books, config, orchestrator, logger))

    to_reset = results['to_reset']
    dry_run = args.dry_run

    # Apply resets
    if to_reset and not dry_run:
        logger.info(f"Resetting {len(to_reset)} deleted book(s) in DB...")
        for barcode in to_reset:
            state_manager.reset_book_migration(barcode)
            logger.info(f"  Reset {barcode}")

    # Summary
    logger.info("=" * 60)
    logger.info("Reconciliation Summary")
    logger.info("=" * 60)
    logger.info(f"  Checked:             {len(books)}")
    logger.info(f"  Found on platform:   {results['found']}")
    logger.info(f"  Deleted (reset):     {len(to_reset)}" + (" [DRY RUN — not applied]" if dry_run else ""))
    logger.info(f"  Skipped (errors):    {results['errors']}")
    if results['error_counts']:
        logger.info("  Error breakdown:")
        for reason, count in sorted(results['error_counts'].items(), key=lambda x: -x[1]):
            logger.info(f"    {reason}: {count}")
    logger.info("=" * 60)

    if to_reset and not dry_run:
        logger.info(f"{len(to_reset)} book(s) reset to 'pending'. Run 'migrate' to re-upload them.")
    elif to_reset and dry_run:
        logger.info(f"Re-run without --dry-run to apply the {len(to_reset)} reset(s).")


async def _reconcile_async(books: list, config, orchestrator, logger) -> dict:
    """Check each book against the platform API using aiohttp."""
    import aiohttp

    base_url = config.api_base_url.rstrip('/')
    max_retries = config.max_retries
    total = len(books)
    found = 0
    errors = 0
    error_counts: dict = {}
    to_reset = []

    logger.info(f"Checking {total} book(s) against the platform...")

    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout, headers={'User-Agent': 'SourceLibrary-MCP/'}) as session:
        for idx, book in enumerate(books, 1):
            if orchestrator.shutdown_requested:
                logger.info("Shutdown requested — stopping reconcile.")
                break

            barcode = book['picturae_barcode']
            api_book_id = book['api_book_id']
            url = f"{base_url}/api/books/{api_book_id}"

            status = None
            for attempt in range(1, max_retries + 1):
                try:
                    async with session.get(url) as response:
                        status = response.status

                        if status == 429:
                            logger.warning(f"[{idx}/{total}] Rate limited — retrying {attempt}/{max_retries}")
                            continue

                        break  # got a definitive response

                except Exception as e:
                    if attempt < max_retries:
                        continue
                    errors += 1
                    key = type(e).__name__
                    error_counts[key] = error_counts.get(key, 0) + 1
                    logger.warning(f"[{idx}/{total}] SKIP    {barcode} — {key}: {e}")
                    status = None
                    break

            if status == 200:
                found += 1
                logger.info(f"[{idx}/{total}] OK      {barcode}")
            elif status == 404:
                to_reset.append(barcode)
                logger.info(f"[{idx}/{total}] DELETED {barcode} — will reset")
            elif status == 429:
                # Still rate limited after all retries
                errors += 1
                key = "HTTP 429"
                error_counts[key] = error_counts.get(key, 0) + 1
                logger.warning(f"[{idx}/{total}] SKIP    {barcode} — still rate limited after {max_retries} retries")
            elif status is not None:
                errors += 1
                key = f"HTTP {status}"
                error_counts[key] = error_counts.get(key, 0) + 1
                logger.warning(f"[{idx}/{total}] SKIP    {barcode} — {key}")

    return {
        'found': found,
        'to_reset': to_reset,
        'errors': errors,
        'error_counts': error_counts,
    }


if __name__ == '__main__':
    main()
