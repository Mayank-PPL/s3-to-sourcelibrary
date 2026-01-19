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


if __name__ == '__main__':
    main()
