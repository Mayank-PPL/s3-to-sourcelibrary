"""Configuration management for the migration tool."""

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv


@dataclass
class Config:
    """Configuration for S3 to Source Library migration."""

    # S3 Configuration
    aws_access_key_id: str
    aws_secret_access_key: str
    aws_region: str
    s3_bucket_name: str
    s3_base_prefix: str

    # API Configuration
    api_base_url: str
    sl_api_secret: str

    # CSV Configuration
    books_csv_path: str
    pages_csv_path: str

    # Migration Settings
    book_workers: int
    max_retries: int
    retry_backoff: float
    request_delay: float  # Delay in seconds between consecutive API requests

    # Paths
    temp_dir: str
    state_db_path: str
    log_file: str

    # Logging
    log_level: str


def load_config(env_file: str = ".env") -> Config:
    """
    Load configuration from .env file.

    Args:
        env_file: Path to .env file (default: ".env")

    Returns:
        Config object with loaded settings

    Raises:
        ValueError: If required configuration is missing
    """
    # Load .env file
    load_dotenv(env_file)

    # Helper function to get required env var
    def get_required(key: str) -> str:
        value = os.getenv(key)
        if value is None:
            raise ValueError(f"Required configuration '{key}' is missing from .env file")
        return value

    # Helper function to get optional env var with default
    def get_optional(key: str, default: str) -> str:
        return os.getenv(key, default)

    # Create config
    config = Config(
        # S3
        aws_access_key_id=get_required("AWS_ACCESS_KEY_ID"),
        aws_secret_access_key=get_required("AWS_SECRET_ACCESS_KEY"),
        aws_region=get_optional("AWS_REGION", "eu-central-1"),
        s3_bucket_name=get_required("S3_BUCKET_NAME"),
        s3_base_prefix=get_optional("S3_BASE_PREFIX", "collection/export_dam_files/jp2"),

        # API
        api_base_url=get_required("API_BASE_URL"),
        sl_api_secret=get_required("SL_API_SECRET"),

        # CSV
        books_csv_path=get_optional("BOOKS_CSV_PATH", "./data/csv/ScannedBooks.csv"),
        pages_csv_path=get_optional("PAGES_CSV_PATH", "./data/csv/PageScans.csv.zip"),

        # Migration
        book_workers=int(get_optional("BOOK_WORKERS", "1")),
        max_retries=int(get_optional("MAX_RETRIES", "3")),
        retry_backoff=float(get_optional("RETRY_BACKOFF", "2.0")),
        request_delay=float(get_optional("REQUEST_DELAY", "1.0")),  # 1 second delay between requests

        # Paths
        temp_dir=get_optional("TEMP_DIR", "./temp"),
        state_db_path=get_optional("STATE_DB_PATH", "./data/index/migration_state.db"),
        log_file=get_optional("LOG_FILE", "./logs/migration.log"),

        # Logging
        log_level=get_optional("LOG_LEVEL", "INFO"),
    )

    # Validate paths exist (create if needed)
    _validate_and_create_paths(config)

    return config


def _validate_and_create_paths(config: Config) -> None:
    """
    Validate configuration and create necessary directories.

    Args:
        config: Configuration object
    """
    from src.utils import ensure_directory_exists

    # Ensure directories exist
    ensure_directory_exists(Path(config.temp_dir))
    ensure_directory_exists(Path(config.state_db_path).parent)
    ensure_directory_exists(Path(config.log_file).parent)

    # Validate book_workers
    if config.book_workers < 1:
        raise ValueError("BOOK_WORKERS must be at least 1")

    # Validate max_retries
    if config.max_retries < 0:
        raise ValueError("MAX_RETRIES must be non-negative")

    # Validate API URLs
    if not config.api_base_url.startswith(("http://", "https://")):
        raise ValueError("API_BASE_URL must start with http:// or https://")
