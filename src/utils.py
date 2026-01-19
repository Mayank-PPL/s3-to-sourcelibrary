"""Utility functions for logging, directory management, and file operations."""

import logging
import os
import sys
from pathlib import Path
from typing import Optional


def setup_logging(log_file: str, log_level: str = "INFO") -> logging.Logger:
    """
    Setup logging configuration.

    Args:
        log_file: Path to log file
        log_level: Logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL)

    Returns:
        Configured logger instance
    """
    # Ensure logs directory exists
    log_dir = Path(log_file).parent
    ensure_directory_exists(log_dir)

    # Create logger
    logger = logging.getLogger("migration")
    logger.setLevel(getattr(logging, log_level.upper()))

    # Remove existing handlers
    logger.handlers.clear()

    # Console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_format = logging.Formatter(
        '%(asctime)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    console_handler.setFormatter(console_format)

    # File handler
    file_handler = logging.FileHandler(log_file)
    file_handler.setLevel(logging.DEBUG)
    file_format = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(funcName)s:%(lineno)d - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    file_handler.setFormatter(file_format)

    logger.addHandler(console_handler)
    logger.addHandler(file_handler)

    return logger


def ensure_directory_exists(directory: Path | str) -> None:
    """
    Ensure a directory exists, create if it doesn't.

    Args:
        directory: Path to directory
    """
    path = Path(directory)
    if not path.exists():
        path.mkdir(parents=True, exist_ok=True)


def get_temp_filepath(barcode: str, filename: str, temp_dir: str) -> str:
    """
    Generate a unique temporary filepath for downloaded S3 files.

    Args:
        barcode: Book barcode
        filename: Original filename
        temp_dir: Temporary directory path

    Returns:
        Full path to temporary file
    """
    ensure_directory_exists(temp_dir)
    # Use barcode prefix to avoid conflicts between concurrent books
    temp_filename = f"{barcode}_{filename}"
    return os.path.join(temp_dir, temp_filename)


def cleanup_temp_file(filepath: str, logger: Optional[logging.Logger] = None) -> None:
    """
    Delete a temporary file, logging any errors.

    Args:
        filepath: Path to file to delete
        logger: Logger instance
    """
    try:
        if os.path.exists(filepath):
            os.remove(filepath)
            if logger:
                logger.debug(f"Deleted temp file: {filepath}")
    except Exception as e:
        if logger:
            logger.warning(f"Failed to delete temp file {filepath}: {e}")


def extract_sequence_from_filename(filename: str) -> int:
    """
    Extract sequence number from filename.

    Examples:
        "RIT001001887_0001.jp2" -> 1
        "RIT001001887_0042.jp2" -> 42

    Args:
        filename: Filename with embedded sequence number

    Returns:
        Sequence number as integer
    """
    import re
    # Match pattern like _0001, _0042, etc.
    match = re.search(r'_(\d+)', filename)
    if match:
        return int(match.group(1))
    return 0  # Default if no match


def format_bytes(size_bytes: int) -> str:
    """
    Format bytes to human-readable format.

    Args:
        size_bytes: Size in bytes

    Returns:
        Formatted string (e.g., "1.5 GB")
    """
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if size_bytes < 1024.0:
            return f"{size_bytes:.2f} {unit}"
        size_bytes /= 1024.0
    return f"{size_bytes:.2f} PB"


def format_duration(seconds: float) -> str:
    """
    Format duration in seconds to human-readable format.

    Args:
        seconds: Duration in seconds

    Returns:
        Formatted string (e.g., "2h 15m 30s")
    """
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)

    parts = []
    if hours > 0:
        parts.append(f"{hours}h")
    if minutes > 0:
        parts.append(f"{minutes}m")
    if secs > 0 or not parts:
        parts.append(f"{secs}s")

    return " ".join(parts)
