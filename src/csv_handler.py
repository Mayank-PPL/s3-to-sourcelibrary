"""CSV parsing for Books and Pages metadata."""

import logging
import zipfile
from pathlib import Path
from typing import Any, Dict, Iterator, Optional

import pandas as pd

from src.utils import extract_sequence_from_filename


# Required columns from Books CSV
BOOKS_CSV_COLUMNS = [
    'Picturae barcode',
    'Title',
    'Author',
    'Editor',
    'Place of publication',
    'Printer',
    'Publisher',
    'Year of publication',
    'Language'
]

# Required columns from Pages CSV
PAGES_CSV_COLUMNS = ['Code', 'Filename', 'DAM Directory']


class CSVHandler:
    """Handles parsing of Books and Pages CSV files."""

    def __init__(self, logger: logging.Logger):
        """
        Initialize CSVHandler.

        Args:
            logger: Logger instance
        """
        self.logger = logger

    def _get_csv_from_zip(self, zip_path: str) -> str:
        """
        Extract the actual CSV filename from a ZIP file, ignoring macOS metadata files.

        Args:
            zip_path: Path to ZIP file

        Returns:
            Name of the CSV file inside the ZIP

        Raises:
            ValueError: If no valid CSV found or multiple CSVs found
        """
        with zipfile.ZipFile(zip_path, 'r') as zf:
            # Get all files in the ZIP
            all_files = zf.namelist()

            # Filter out macOS metadata files and directories
            csv_files = [
                f for f in all_files
                if f.endswith('.csv')
                and not f.startswith('__MACOSX')
                and not f.startswith('.')
                and '/' not in f  # Only top-level files
            ]

            if len(csv_files) == 0:
                raise ValueError(f"No CSV file found in ZIP: {zip_path}")
            elif len(csv_files) > 1:
                raise ValueError(f"Multiple CSV files found in ZIP: {csv_files}")

            return csv_files[0]

    def parse_books_csv(self, csv_path: str) -> Iterator[Dict]:
        """
        Parse Books CSV file in ascending order of Picturae barcode.

        Args:
            csv_path: Path to Books CSV file

        Yields:
            Dictionary with 'barcode' and 'metadata' keys
        """
        self.logger.info(f"Parsing Books CSV from: {csv_path}")

        if not Path(csv_path).exists():
            raise FileNotFoundError(f"Books CSV not found: {csv_path}")

        # Read CSV
        try:
            df = pd.read_csv(csv_path, encoding='utf-8')
        except UnicodeDecodeError:
            # Try with different encoding
            df = pd.read_csv(csv_path, encoding='latin-1')

        self.logger.info(f"Found {len(df)} books in CSV")

        # Check for required column
        if 'Picturae barcode' not in df.columns:
            raise ValueError("Books CSV missing required column: 'Picturae barcode'")

        # Sort by Picturae barcode in ascending order
        df = df.sort_values('Picturae barcode')
        self.logger.info("Sorted books by Picturae barcode (ascending)")

        # Log available columns for reference
        self.logger.debug(f"Available columns: {list(df.columns)}")
        self.logger.info(f"Using metadata columns: {BOOKS_CSV_COLUMNS}")

        # Yield each book
        for idx, row in df.iterrows():
            barcode = str(row['Picturae barcode']).strip()

            # Build metadata dictionary from specified columns
            metadata: Dict[str, Optional[str]] = {
                'picturae_barcode': barcode  # Always include barcode
            }

            for col in BOOKS_CSV_COLUMNS:
                if col == 'Picturae barcode':
                    continue  # Already added

                if col in df.columns:
                    value = row[col]
                    # Convert nan to None
                    if pd.isna(value):
                        metadata[col] = None
                    else:
                        metadata[col] = str(value).strip() if isinstance(value, str) else value
                else:
                    self.logger.warning(f"Column '{col}' not found in Books CSV, will be None")
                    metadata[col] = None

            yield {
                'barcode': barcode,
                'metadata': metadata
            }

    def parse_pages_csv(self, csv_path: str, chunk_size: int = 10000) -> Iterator[Dict]:
        """
        Parse Pages CSV file in chunks.
        Uses Code, Filename, and DAM Directory columns.

        Args:
            csv_path: Path to Pages CSV file (can be .csv or .zip)
            chunk_size: Number of rows to process at a time

        Yields:
            Dictionary with page information
        """
        self.logger.info(f"Parsing Pages CSV from: {csv_path}")

        if not Path(csv_path).exists():
            raise FileNotFoundError(f"Pages CSV not found: {csv_path}")

        # Handle ZIP files with macOS metadata
        read_path = csv_path
        compression = None

        if csv_path.endswith('.gz'):
            compression = 'gzip'
        elif csv_path.endswith('.zip'):
            # Get the actual CSV filename from the ZIP
            csv_filename = self._get_csv_from_zip(csv_path)
            self.logger.info(f"Found CSV in ZIP: {csv_filename}")
            # Construct the path pandas needs: zip://filename.csv::archive.zip
            read_path = f"zip://{csv_filename}::{csv_path}"
            compression = None  # pandas handles this with the zip:// protocol

        # Specify dtypes for only the columns we need to avoid pandas type inference overhead
        # Using string literals for pandas dtype specification
        dtype_spec = {
            'Code': 'str',
            'Filename': 'str',
            'DAM Directory': 'str'
        }

        # Read CSV in chunks
        try:
            chunks = pd.read_csv(
                read_path,
                encoding='utf-8',
                compression=compression,
                chunksize=chunk_size,
                usecols=PAGES_CSV_COLUMNS,  # Only read columns we need
                dtype=dtype_spec  # type: ignore[arg-type]
            )
        except UnicodeDecodeError:
            chunks = pd.read_csv(
                read_path,
                encoding='latin-1',
                compression=compression,
                chunksize=chunk_size,
                usecols=PAGES_CSV_COLUMNS,
                dtype=dtype_spec  # type: ignore[arg-type]
            )

        total_rows = 0

        for chunk in chunks:
            # Check for required columns
            missing_cols = [col for col in PAGES_CSV_COLUMNS if col not in chunk.columns]
            if missing_cols:
                raise ValueError(f"Pages CSV missing required columns: {missing_cols}")

            # Process each row in chunk
            for idx, row in chunk.iterrows():
                barcode = str(row['Code']).strip()
                filename = str(row['Filename']).strip()
                dam_directory = str(row['DAM Directory']).strip() if pd.notna(row['DAM Directory']) else ""

                # Extract sequence number from filename (filenames don't include extensions in CSV)
                sequence_order = extract_sequence_from_filename(filename)

                total_rows += 1

                yield {
                    'barcode': barcode,
                    'filename': filename,
                    'dam_directory': dam_directory,
                    'sequence_order': sequence_order
                }

            # Log progress periodically
            if total_rows % 50000 == 0:
                self.logger.info(f"Processed {total_rows} pages...")

        self.logger.info(f"Completed parsing {total_rows} total pages")

    def count_pages_by_book(self, csv_path: str, chunk_size: int = 10000) -> Dict[str, int]:
        """
        Count pages per book from Pages CSV.

        Args:
            csv_path: Path to Pages CSV file
            chunk_size: Number of rows to process at a time

        Returns:
            Dictionary mapping barcode to page count
        """
        self.logger.info("Counting pages per book...")

        page_counts: Dict[str, int] = {}

        # Handle ZIP files with macOS metadata
        read_path = csv_path
        compression = None

        if csv_path.endswith('.gz'):
            compression = 'gzip'
        elif csv_path.endswith('.zip'):
            # Get the actual CSV filename from the ZIP
            csv_filename = self._get_csv_from_zip(csv_path)
            self.logger.info(f"Found CSV in ZIP: {csv_filename}")
            # Construct the path pandas needs: zip://filename.csv::archive.zip
            read_path = f"zip://{csv_filename}::{csv_path}"
            compression = None  # pandas handles this with the zip:// protocol

        # Only read the Code column we need for counting
        dtype_spec = {'Code': 'str'}

        # Read CSV in chunks
        try:
            chunks = pd.read_csv(
                read_path,
                encoding='utf-8',
                compression=compression,
                chunksize=chunk_size,
                usecols=['Code'],
                dtype=dtype_spec  # type: ignore[arg-type]
            )
        except UnicodeDecodeError:
            chunks = pd.read_csv(
                read_path,
                encoding='latin-1',
                compression=compression,
                chunksize=chunk_size,
                usecols=['Code'],
                dtype=dtype_spec  # type: ignore[arg-type]
            )

        for chunk in chunks:
            if 'Code' not in chunk.columns:
                raise ValueError("Pages CSV missing 'Code' column")

            # Count occurrences of each Code (barcode)
            counts = chunk['Code'].value_counts().to_dict()

            # Merge with existing counts
            for barcode, count in counts.items():
                barcode_str = str(barcode).strip()
                page_counts[barcode_str] = page_counts.get(barcode_str, 0) + count

        self.logger.info(f"Found pages for {len(page_counts)} unique books")

        return page_counts
