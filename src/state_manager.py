"""SQLite database management for indexing and migration state tracking."""

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from src.utils import ensure_directory_exists


class StateManager:
    """Manages SQLite database for indexing and migration state."""

    def __init__(self, db_path: str):
        """
        Initialize StateManager.

        Args:
            db_path: Path to SQLite database file
        """
        self.db_path = db_path
        ensure_directory_exists(Path(db_path).parent)
        self._init_database()

    @contextmanager
    def get_connection(self):
        """Context manager for database connections."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_database(self) -> None:
        """Initialize database schema if not exists."""
        with self.get_connection() as conn:
            cursor = conn.cursor()

            # Enable WAL mode for better concurrency with multiple workers
            cursor.execute("PRAGMA journal_mode = WAL")
            cursor.execute("PRAGMA busy_timeout = 5000")  # 5 second wait before error
            cursor.execute("PRAGMA synchronous = NORMAL")  # Faster with WAL, still safe

            # Books table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS books (
                    picturae_barcode TEXT PRIMARY KEY,
                    metadata_json TEXT,
                    api_book_id TEXT,
                    migration_status TEXT DEFAULT 'pending',
                    total_pages INTEGER DEFAULT 0,
                    uploaded_pages INTEGER DEFAULT 0,
                    indexed_at TIMESTAMP,
                    migration_started_at TIMESTAMP,
                    migration_completed_at TIMESTAMP,
                    error_message TEXT
                )
            """)
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_books_migration_status ON books(migration_status)")

            # Pages table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS pages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    picturae_barcode TEXT NOT NULL,
                    filename TEXT NOT NULL,
                    dam_directory TEXT,
                    s3_key TEXT UNIQUE,
                    file_size_bytes INTEGER,
                    sequence_order INTEGER,
                    upload_status TEXT DEFAULT 'pending',
                    api_page_id TEXT,
                    error_message TEXT,
                    attempt_count INTEGER DEFAULT 0,
                    indexed_at TIMESTAMP,
                    uploaded_at TIMESTAMP,
                    FOREIGN KEY (picturae_barcode) REFERENCES books(picturae_barcode)
                )
            """)
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_pages_status ON pages(upload_status)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_pages_barcode ON pages(picturae_barcode)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_pages_barcode_status ON pages(picturae_barcode, upload_status)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_pages_sequence ON pages(picturae_barcode, sequence_order)")

            # Indexing metadata table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS indexing_metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT,
                    updated_at TIMESTAMP
                )
            """)

            # Missing files table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS missing_files (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    picturae_barcode TEXT,
                    filename TEXT,
                    dam_directory TEXT,
                    reason TEXT,
                    discovered_at TIMESTAMP
                )
            """)

    # === Books Operations ===

    def insert_book(self, barcode: str, metadata: Dict, total_pages: int = 0) -> None:
        """
        Insert a new book into the database.

        Args:
            barcode: Picturae barcode
            metadata: Book metadata as dictionary
            total_pages: Total number of pages for this book
        """
        with self.get_connection() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO books (picturae_barcode, metadata_json, total_pages, indexed_at)
                VALUES (?, ?, ?, ?)
            """, (barcode, json.dumps(metadata), total_pages, datetime.now()))

    def get_book(self, barcode: str) -> Optional[Dict]:
        """
        Get book by barcode.

        Args:
            barcode: Picturae barcode

        Returns:
            Book record as dictionary or None
        """
        with self.get_connection() as conn:
            cursor = conn.execute("SELECT * FROM books WHERE picturae_barcode = ?", (barcode,))
            row = cursor.fetchone()
            return dict(row) if row else None

    def update_book_api_id(self, barcode: str, api_book_id: str) -> None:
        """Update book with API book ID after creation."""
        with self.get_connection() as conn:
            conn.execute("""
                UPDATE books
                SET api_book_id = ?, migration_status = 'in_progress', migration_started_at = ?
                WHERE picturae_barcode = ?
            """, (api_book_id, datetime.now(), barcode))

    def update_book_status(self, barcode: str, status: str, error_message: Optional[str] = None) -> None:
        """Update book migration status."""
        with self.get_connection() as conn:
            if status == 'completed':
                conn.execute("""
                    UPDATE books
                    SET migration_status = ?, error_message = ?, migration_completed_at = ?
                    WHERE picturae_barcode = ?
                """, (status, error_message, datetime.now(), barcode))
            else:
                conn.execute("""
                    UPDATE books
                    SET migration_status = ?, error_message = ?
                    WHERE picturae_barcode = ?
                """, (status, error_message, barcode))

    def increment_uploaded_pages(self, barcode: str) -> None:
        """Increment uploaded pages count for a book."""
        with self.get_connection() as conn:
            conn.execute("""
                UPDATE books
                SET uploaded_pages = uploaded_pages + 1
                WHERE picturae_barcode = ?
            """, (barcode,))

    def get_books_by_status(self, status: str) -> List[Dict]:
        """Get all books with given status."""
        with self.get_connection() as conn:
            cursor = conn.execute("SELECT * FROM books WHERE migration_status = ?", (status,))
            return [dict(row) for row in cursor.fetchall()]

    def get_all_books(self, limit: Optional[int] = None) -> List[Dict]:
        """Get all books, optionally limited."""
        with self.get_connection() as conn:
            if limit:
                cursor = conn.execute("SELECT * FROM books ORDER BY picturae_barcode LIMIT ?", (limit,))
            else:
                cursor = conn.execute("SELECT * FROM books ORDER BY picturae_barcode")
            return [dict(row) for row in cursor.fetchall()]

    def get_books_by_status(self, statuses: List[str], limit: Optional[int] = None) -> List[Dict]:
        """
        Get books filtered by status.

        Args:
            statuses: List of statuses to filter by (e.g., ['pending', 'in_progress'])
            limit: Optional limit on number of books to return

        Returns:
            List of book records matching the given statuses
        """
        with self.get_connection() as conn:
            placeholders = ','.join('?' * len(statuses))
            query = f"SELECT * FROM books WHERE migration_status IN ({placeholders}) ORDER BY picturae_barcode"

            if limit:
                query += " LIMIT ?"
                cursor = conn.execute(query, (*statuses, limit))
            else:
                cursor = conn.execute(query, statuses)

            return [dict(row) for row in cursor.fetchall()]

    # === Pages Operations ===

    def insert_page(self, barcode: str, filename: str, dam_directory: str,
                    s3_key: str, sequence_order: int, file_size_bytes: int = 0) -> None:
        """Insert a new page into the database."""
        with self.get_connection() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO pages
                (picturae_barcode, filename, dam_directory, s3_key, sequence_order, file_size_bytes, indexed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (barcode, filename, dam_directory, s3_key, sequence_order, file_size_bytes, datetime.now()))

    def get_pages_for_book(self, barcode: str, status: Optional[str] = None) -> List[Dict]:
        """
        Get pages for a book, optionally filtered by status.

        Args:
            barcode: Picturae barcode
            status: Optional upload status filter

        Returns:
            List of page records ordered by sequence
        """
        with self.get_connection() as conn:
            if status:
                cursor = conn.execute("""
                    SELECT * FROM pages
                    WHERE picturae_barcode = ? AND upload_status = ?
                    ORDER BY sequence_order
                """, (barcode, status))
            else:
                cursor = conn.execute("""
                    SELECT * FROM pages
                    WHERE picturae_barcode = ?
                    ORDER BY sequence_order
                """, (barcode,))
            return [dict(row) for row in cursor.fetchall()]

    def update_page_uploaded(self, page_id: int, api_page_id: Optional[str] = None) -> None:
        """Mark page as uploaded."""
        with self.get_connection() as conn:
            conn.execute("""
                UPDATE pages
                SET upload_status = 'uploaded', api_page_id = ?, uploaded_at = ?
                WHERE id = ?
            """, (api_page_id, datetime.now(), page_id))

    def update_page_failed(self, page_id: int, error_message: str) -> None:
        """Mark page as failed with error message."""
        with self.get_connection() as conn:
            conn.execute("""
                UPDATE pages
                SET upload_status = 'failed', error_message = ?, attempt_count = attempt_count + 1
                WHERE id = ?
            """, (error_message, page_id))

    def reset_page_status(self, page_id: int) -> None:
        """Reset page status to pending."""
        with self.get_connection() as conn:
            conn.execute("""
                UPDATE pages
                SET upload_status = 'pending', error_message = NULL, attempt_count = 0
                WHERE id = ?
            """, (page_id,))

    def count_uploaded_pages_for_book(self, barcode: str) -> int:
        """
        Count successfully uploaded pages for a specific book.

        Args:
            barcode: Picturae barcode

        Returns:
            Number of uploaded pages
        """
        with self.get_connection() as conn:
            cursor = conn.execute("""
                SELECT COUNT(*) FROM pages
                WHERE picturae_barcode = ? AND upload_status = 'uploaded'
            """, (barcode,))
            return cursor.fetchone()[0]

    # === Statistics ===

    def get_migration_statistics(self) -> Dict:
        """Get overall migration statistics."""
        with self.get_connection() as conn:
            cursor = conn.cursor()

            # Book statistics
            cursor.execute("SELECT COUNT(*) FROM books")
            total_books = cursor.fetchone()[0]

            cursor.execute("SELECT COUNT(*) FROM books WHERE migration_status = 'completed'")
            completed_books = cursor.fetchone()[0]

            cursor.execute("SELECT COUNT(*) FROM books WHERE migration_status = 'failed'")
            failed_books = cursor.fetchone()[0]

            cursor.execute("SELECT COUNT(*) FROM books WHERE migration_status = 'in_progress'")
            in_progress_books = cursor.fetchone()[0]

            # Page statistics
            cursor.execute("SELECT COUNT(*) FROM pages")
            total_pages = cursor.fetchone()[0]

            cursor.execute("SELECT COUNT(*) FROM pages WHERE upload_status = 'uploaded'")
            uploaded_pages = cursor.fetchone()[0]

            cursor.execute("SELECT COUNT(*) FROM pages WHERE upload_status = 'failed'")
            failed_pages = cursor.fetchone()[0]

            cursor.execute("SELECT SUM(file_size_bytes) FROM pages WHERE upload_status = 'uploaded'")
            uploaded_bytes = cursor.fetchone()[0] or 0

            cursor.execute("SELECT SUM(file_size_bytes) FROM pages")
            total_bytes = cursor.fetchone()[0] or 0

            return {
                "total_books": total_books,
                "completed_books": completed_books,
                "failed_books": failed_books,
                "in_progress_books": in_progress_books,
                "pending_books": total_books - completed_books - failed_books - in_progress_books,
                "total_pages": total_pages,
                "uploaded_pages": uploaded_pages,
                "failed_pages": failed_pages,
                "pending_pages": total_pages - uploaded_pages - failed_pages,
                "uploaded_bytes": uploaded_bytes,
                "total_bytes": total_bytes,
            }

    # === Indexing Metadata ===

    def set_metadata(self, key: str, value: str) -> None:
        """Set indexing metadata."""
        with self.get_connection() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO indexing_metadata (key, value, updated_at)
                VALUES (?, ?, ?)
            """, (key, value, datetime.now()))

    def get_metadata(self, key: str) -> Optional[str]:
        """Get indexing metadata."""
        with self.get_connection() as conn:
            cursor = conn.execute("SELECT value FROM indexing_metadata WHERE key = ?", (key,))
            row = cursor.fetchone()
            return row[0] if row else None

    def is_indexed(self) -> bool:
        """Check if database has been indexed."""
        completed = self.get_metadata("indexing_completed")
        return completed == "true"

    def mark_indexing_completed(self) -> None:
        """Mark indexing as completed."""
        self.set_metadata("indexing_completed", "true")
        self.set_metadata("indexing_completed_at", datetime.now().isoformat())

    # === Missing Files ===

    def add_missing_file(self, barcode: str, filename: str, dam_directory: str, reason: str) -> None:
        """Record a missing file."""
        with self.get_connection() as conn:
            conn.execute("""
                INSERT INTO missing_files (picturae_barcode, filename, dam_directory, reason, discovered_at)
                VALUES (?, ?, ?, ?, ?)
            """, (barcode, filename, dam_directory, reason, datetime.now()))

    def get_missing_files_count(self) -> int:
        """Get count of missing files."""
        with self.get_connection() as conn:
            cursor = conn.execute("SELECT COUNT(*) FROM missing_files")
            return cursor.fetchone()[0]

    # === Reset Operations ===

    def reset_book_migration(self, barcode: str) -> None:
        """Reset migration state for a specific book."""
        with self.get_connection() as conn:
            conn.execute("""
                UPDATE books
                SET migration_status = 'pending', api_book_id = NULL,
                    uploaded_pages = 0, migration_started_at = NULL,
                    migration_completed_at = NULL, error_message = NULL
                WHERE picturae_barcode = ?
            """, (barcode,))

            conn.execute("""
                UPDATE pages
                SET upload_status = 'pending', api_page_id = NULL,
                    error_message = NULL, attempt_count = 0, uploaded_at = NULL
                WHERE picturae_barcode = ?
            """, (barcode,))

    def reset_all_migration_state(self) -> None:
        """Reset all migration state (keeps index)."""
        with self.get_connection() as conn:
            conn.execute("""
                UPDATE books
                SET migration_status = 'pending', api_book_id = NULL,
                    uploaded_pages = 0, migration_started_at = NULL,
                    migration_completed_at = NULL, error_message = NULL
            """)

            conn.execute("""
                UPDATE pages
                SET upload_status = 'pending', api_page_id = NULL,
                    error_message = NULL, attempt_count = 0, uploaded_at = NULL
            """)

    def delete_all_data(self) -> None:
        """Delete all data from database (full reset)."""
        with self.get_connection() as conn:
            conn.execute("DELETE FROM pages")
            conn.execute("DELETE FROM books")
            conn.execute("DELETE FROM indexing_metadata")
            conn.execute("DELETE FROM missing_files")
