"""S3 file location and download operations."""

import logging
from typing import List, Optional, Tuple

import boto3
from botocore.exceptions import ClientError

from src.utils import cleanup_temp_file, get_temp_filepath


# Common image file extensions to try
IMAGE_EXTENSIONS = ['.jp2', '.jpg', '.jpeg', '.tif', '.tiff', '.png']


class S3Locator:
    """Handles S3 file location and download operations."""

    def __init__(self, config, logger: logging.Logger):
        """
        Initialize S3Locator.

        Args:
            config: Configuration object
            logger: Logger instance
        """
        self.config = config
        self.logger = logger

        # Initialize S3 client
        self.s3_client = boto3.client(
            's3',
            aws_access_key_id=config.aws_access_key_id,
            aws_secret_access_key=config.aws_secret_access_key,
            region_name=config.aws_region
        )

        self.bucket_name = config.s3_bucket_name
        self.base_prefix = config.s3_base_prefix

    def construct_s3_key(self, dam_directory: str, filename: str, extension: str = '') -> str:
        """
        Construct S3 key from DAM Directory and filename.

        Args:
            dam_directory: DAM Directory path (e.g., "/PicturaeScans/Batch_8/RIT001001887")
            filename: Filename without extension (e.g., "RIT001001887_0001")
            extension: File extension to append (e.g., ".jp2")

        Returns:
            Full S3 key
        """
        # Remove leading slash from dam_directory
        dam_dir_clean = dam_directory.lstrip('/')

        # Add extension if provided
        filename_with_ext = f"{filename}{extension}" if extension else filename

        # Construct full S3 key
        s3_key = f"{self.base_prefix}/{dam_dir_clean}/{filename_with_ext}"

        return s3_key

    def check_file_exists(self, s3_key: str) -> Tuple[bool, int]:
        """
        Check if file exists in S3 and get its size.

        Args:
            s3_key: S3 object key

        Returns:
            Tuple of (exists: bool, size_bytes: int)
        """
        try:
            response = self.s3_client.head_object(Bucket=self.bucket_name, Key=s3_key)
            size_bytes = response.get('ContentLength', 0)
            return True, size_bytes
        except ClientError as e:
            error_code = e.response.get('Error', {}).get('Code', '')
            if error_code == '404':
                return False, 0
            else:
                # Other error
                self.logger.warning(f"Error checking S3 file {s3_key}: {e}")
                return False, 0

    def generate_presigned_url(self, s3_key: str, expiration: int = 3600) -> Optional[str]:
        """
        Generate presigned HTTPS URL for S3 object.

        Args:
            s3_key: S3 object key
            expiration: URL expiration in seconds (default: 3600 = 1 hour)

        Returns:
            Presigned URL string or None if failed
        """
        try:
            url = self.s3_client.generate_presigned_url(
                'get_object',
                Params={'Bucket': self.bucket_name, 'Key': s3_key},
                ExpiresIn=expiration
            )
            self.logger.debug(f"Generated presigned URL for {s3_key} (expires in {expiration}s)")
            return url
        except ClientError as e:
            self.logger.error(f"Failed to generate presigned URL for {s3_key}: {e}")
            return None

    def find_file_with_extension(self, dam_directory: str, filename: str) -> Tuple[Optional[str], int, Optional[str]]:
        """
        Find file by trying multiple extensions.

        Args:
            dam_directory: DAM Directory path
            filename: Filename without extension

        Returns:
            Tuple of (s3_key or None, file_size_bytes, extension_found or None)
        """
        # Try each extension
        for ext in IMAGE_EXTENSIONS:
            s3_key = self.construct_s3_key(dam_directory, filename, ext)
            exists, size_bytes = self.check_file_exists(s3_key)

            if exists:
                self.logger.debug(f"Found file with extension {ext}: {s3_key}")
                return s3_key, size_bytes, ext

        # File not found with any extension
        return None, 0, None

    def download_file(self, s3_key: str, barcode: str, filename: str) -> Optional[str]:
        """
        Download file from S3 to temp directory.

        Args:
            s3_key: S3 object key
            barcode: Book barcode (for temp filename uniqueness)
            filename: Filename (will extract actual name from s3_key if needed)

        Returns:
            Path to downloaded temp file, or None if failed
        """
        # Extract actual filename with extension from s3_key
        actual_filename = s3_key.split('/')[-1]

        # Generate temp filepath
        temp_filepath = get_temp_filepath(barcode, actual_filename, self.config.temp_dir)

        try:
            self.logger.debug(f"Downloading {s3_key} to {temp_filepath}")
            self.s3_client.download_file(self.bucket_name, s3_key, temp_filepath)
            return temp_filepath
        except ClientError as e:
            self.logger.error(f"Failed to download {s3_key}: {e}")
            # Cleanup partial download
            cleanup_temp_file(temp_filepath, self.logger)
            return None
        except Exception as e:
            self.logger.error(f"Unexpected error downloading {s3_key}: {e}")
            cleanup_temp_file(temp_filepath, self.logger)
            return None

    def verify_and_get_file_info(self, dam_directory: str, filename: str) -> Tuple[Optional[str], int]:
        """
        Verify file exists and get info during indexing.
        Tries multiple extensions since filename doesn't include extension.

        Args:
            dam_directory: DAM Directory path
            filename: Filename without extension

        Returns:
            Tuple of (s3_key or None, file_size_bytes)
        """
        # Try to find file with any of the common extensions
        s3_key, size_bytes, ext_found = self.find_file_with_extension(dam_directory, filename)

        if s3_key:
            return s3_key, size_bytes
        else:
            # File not found at expected location
            self.logger.warning(f"File not found with any extension: {dam_directory}/{filename}")
            return None, 0
