"""
Base HTTP downloader with streaming support and best practices.

This module provides a robust foundation for HTTP-based media downloads,
implementing all the best practices researched for 2024 including streaming,
progress tracking, session management, and proper error handling.
"""

import os
import time
import re
import hashlib
import shutil
import logging
from typing import Optional, Dict, Callable
from urllib.parse import urlparse, unquote
from dataclasses import dataclass

# Modern fast hashing with graceful fallback
try:
    import blake3
    BLAKE3_AVAILABLE = True
except ImportError:
    BLAKE3_AVAILABLE = False
try:
    from curl_cffi import requests
    from curl_cffi.requests import RequestsError, Timeout, ConnectionError
    CURL_CFFI_AVAILABLE = True
except ImportError:
    # Fallback to standard requests if curl_cffi is not available
    import requests
    from requests.exceptions import Timeout, ConnectionError, RequestException as RequestsError
    CURL_CFFI_AVAILABLE = False
from urllib3.util.retry import Retry

# Tenacity for intelligent retry strategies
try:
    from tenacity import (
        retry, stop_after_attempt, wait_exponential, wait_fixed,
        retry_if_exception_type, retry_if_result, before_sleep_log
    )
    import logging
    TENACITY_AVAILABLE = True
except ImportError:
    TENACITY_AVAILABLE = False

from utils.service_abstractions import (
    DownloadResult, DownloadStatus,
    MediaMetadata, MediaType, ServiceConfig
)
from utils.rate_limiter import rate_limit_manager
from utils.url_transformer import url_transformer
from utils.constants import (
    DOWNLOAD_CHUNK_SIZE, DISK_SPACE_SAFETY_FACTOR, MIN_MEDIA_FILE_SIZE,
    SQLITE_CACHE_SIZE_KB, MIN_FREE_SPACE_MB
)

# Optional imports for format-specific validation
try:
    from PIL import Image
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False


@dataclass
class FileIntegrityResult:
    """Result of file integrity validation."""
    is_valid: bool
    error_message: Optional[str] = None
    file_size: Optional[int] = None
    checksum: Optional[str] = None


def should_retry_download_error(exception):
    """
    Determine if we should retry based on the exception type.

    Retry strategy:
    - Connection errors: Yes (network issues)
    - Timeouts: Yes (temporary server issues)
    - Server errors (5xx): Yes (temporary server issues)
    - Rate limits (429): No (handled by rate limiter)
    - Client errors (4xx): No (permanent failures)
    """
    if isinstance(exception, (ConnectionError, Timeout)):
        return True

    if hasattr(exception, 'response') and exception.response is not None:
        status_code = exception.response.status_code
        # Retry on server errors (5xx) but not client errors (4xx)
        return 500 <= status_code < 600

    return False


def create_download_retry_decorator():
    """Create retry decorator with intelligent strategies if tenacity is available."""
    if not TENACITY_AVAILABLE:
        # If tenacity not available, return identity decorator
        def no_retry_decorator(func):
            return func
        return no_retry_decorator

    return retry(
        # Retry conditions
        retry=retry_if_exception_type((ConnectionError, Timeout)) |
              retry_if_exception_type(RequestsError),

        # Stop after 3 attempts for most errors
        stop=stop_after_attempt(3),

        # Exponential backoff: wait 1s, 2s, 4s between retries
        wait=wait_exponential(multiplier=1, min=1, max=10),

        # Log retry attempts
        before_sleep=before_sleep_log(logging.getLogger(__name__), logging.WARNING),

        # Additional condition check
        retry_error_callback=lambda retry_state: should_retry_download_error(retry_state.outcome.exception())
    )


class BaseHTTPDownloader:
    """
    Base class for HTTP-based media downloaders with streaming support.

    Implements web-researched best practices for 2024:
    - Streaming downloads for large files
    - Session management with connection pooling
    - Configurable timeouts (connect/read)
    - Retry logic with exponential backoff
    - Progress tracking support
    - Proper error handling and recovery
    """

    def __init__(self, config: ServiceConfig):
        self.config = config
        self._session = None
        self._last_request_time = 0.0
        self._setup_session()

        # Register with rate limit manager
        rate_limit_manager.register_service_from_config(config.name.lower(), config)

        # Create retry decorator for this instance
        self._retry_decorator = create_download_retry_decorator()

        # Log which hash algorithm is available
        hash_info = "BLAKE3 (fast)" if BLAKE3_AVAILABLE else "SHA256 (fallback)"
        self._logger = logging.getLogger(f"{__name__}.{self.__class__.__name__}")
        self._logger.debug(f"File integrity validation using: {hash_info}")

    def _setup_session(self):
        """Setup HTTP session with browser impersonation and retry strategy."""
        if CURL_CFFI_AVAILABLE:
            # Use curl_cffi with Chrome browser impersonation for better TLS fingerprinting
            self._session = requests.Session(
                impersonate="chrome110",  # Impersonate Chrome 110 for optimal compatibility
                timeout=self.config.timeout_seconds
            )
        else:
            # Fallback to standard requests session
            self._session = requests.Session()

            # Configure retry strategy for standard requests
            retry_strategy = Retry(
                total=3,
                read=3,
                connect=3,
                backoff_factor=0.3,
                status_forcelist=(429, 500, 502, 503, 504)
            )

            from requests.adapters import HTTPAdapter
            adapter = HTTPAdapter(
                max_retries=retry_strategy,
                pool_connections=10,
                pool_maxsize=20
            )

            self._session.mount("http://", adapter)
            self._session.mount("https://", adapter)

        # Set enhanced headers for better compatibility
        # Accept header prioritizes images to prevent Reddit serving HTML wrapper pages
        self._session.headers.update({
            'User-Agent': self.config.user_agent,
            'Accept': 'image/webp,image/apng,image/jpeg,image/png,image/*,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.9',
            'Accept-Encoding': 'gzip, deflate, br',
            'DNT': '1',
            'Connection': 'keep-alive',
            'Upgrade-Insecure-Requests': '1',
            'Cache-Control': 'max-age=0'
        })

    def _respect_rate_limit(self):
        """Enforce rate limiting using the centralized rate limit manager."""
        service_name = self.config.name.lower()

        # Use the sophisticated rate limit manager instead of simple time-based limiting
        # Increased timeout to 90 seconds to handle Imgur's backoff periods
        if not rate_limit_manager.acquire(service_name, timeout=90):
            raise RuntimeError(f"Rate limit timeout for service '{service_name}'")

    def _get_filename_from_url(self, url: str) -> str:
        """Extract filename from URL, handling encoding issues."""
        try:
            parsed = urlparse(url)
            filename = os.path.basename(parsed.path)

            # Handle URL encoding issues (common with Reddit URLs)
            filename = unquote(filename)

            # If no filename in path, generate one from URL
            if not filename or '.' not in filename:
                # Use last part of path or generate from URL hash
                path_parts = parsed.path.strip('/').split('/')
                if path_parts and path_parts[-1]:
                    filename = f"{path_parts[-1]}.unknown"
                else:
                    filename = f"media_{abs(hash(url)) % 10000}.unknown"

            return filename
        except Exception:
            # Fallback to hash-based filename
            return f"media_{abs(hash(url)) % 10000}.unknown"

    def _get_file_extension_from_headers(self, headers: Dict[str, str]) -> Optional[str]:
        """Determine file extension from Content-Type header."""
        content_type = headers.get('content-type', '').lower()

        # Common media type mappings
        type_map = {
            'image/jpeg': '.jpg',
            'image/jpg': '.jpg',
            'image/png': '.png',
            'image/gif': '.gif',
            'image/webp': '.webp',
            'video/mp4': '.mp4',
            'video/webm': '.webm',
            'video/quicktime': '.mov',
            'audio/mpeg': '.mp3',
            'audio/mp4': '.m4a',
            'audio/wav': '.wav'
        }

        for mime_type, ext in type_map.items():
            if mime_type in content_type:
                return ext

        return None

    def _fix_filename_extension(self, filename: str, headers: Dict[str, str]) -> str:
        """Fix filename extension based on Content-Type if needed."""
        # If filename already has a valid extension, keep it
        name, ext = os.path.splitext(filename)
        if ext.lower() in ['.jpg', '.jpeg', '.png', '.gif', '.webp', '.mp4', '.webm', '.mov', '.mp3', '.m4a', '.wav']:
            return filename

        # Try to get extension from headers
        header_ext = self._get_file_extension_from_headers(headers)
        if header_ext:
            return f"{name}{header_ext}"

        return filename

    def _check_disk_space(self, file_size: int, save_path: str) -> bool:
        """
        Check if there's sufficient disk space for download.

        Args:
            file_size: Expected file size in bytes
            save_path: Path where file will be downloaded

        Returns:
            True if sufficient disk space available, False otherwise
        """
        if file_size <= 0:
            return True  # Can't check if size unknown, allow download

        try:
            # Get disk usage for the directory
            directory = os.path.dirname(save_path) or '.'
            usage = shutil.disk_usage(directory)

            # Apply safety factor (configurable extra space required)
            safety_factor = DISK_SPACE_SAFETY_FACTOR
            required_space = int(file_size * safety_factor)

            if usage.free < required_space:
                self._logger.warning(
                    f"Insufficient disk space for {save_path}. "
                    f"Required: {required_space / (1024**3):.2f}GB, "
                    f"Available: {usage.free / (1024**3):.2f}GB"
                )
                return False

            self._logger.debug(
                f"Disk space check passed. "
                f"Required: {required_space / (1024**2):.1f}MB, "
                f"Available: {usage.free / (1024**3):.2f}GB"
            )
            return True

        except (OSError, ValueError) as e:
            self._logger.warning(f"Could not check disk space: {e}")
            # If we can't check disk space, allow download (better than blocking)
            return True

    def _validate_url(self, url: str) -> bool:
        """
        Validate URL to filter out invalid URLs before download attempts.

        Prevents attempting to download:
        - Local filenames (without domains)
        - Malformed URLs
        - URLs with invalid schemes
        - Common invalid patterns

        Args:
            url: URL to validate

        Returns:
            True if URL is valid for downloading, False otherwise
        """
        if not url or not isinstance(url, str):
            return False

        # Remove leading/trailing whitespace
        url = url.strip()

        # Basic length check (URLs shouldn't be too short or extremely long)
        if len(url) < 10 or len(url) > 2048:
            return False

        try:
            parsed = urlparse(url)

            # Must have valid scheme
            if parsed.scheme.lower() not in ['http', 'https']:
                return False

            # Must have a valid netloc (domain)
            if not parsed.netloc:
                return False

            # Check for valid domain structure
            domain = parsed.netloc.lower()

            # Domain must contain at least one dot (except localhost)
            if '.' not in domain and domain != 'localhost':
                return False

            # Check for common invalid patterns
            invalid_patterns = [
                r'^[a-zA-Z]:\\',  # Windows paths (C:\)
                r'^/',            # Unix absolute paths starting with /
                r'^\.',           # Relative paths starting with .
                r'^\w+\.\w+$',    # Just filename.ext without domain
                r'file://',       # File protocol URLs
                r'javascript:',   # JavaScript URLs
                r'data:',         # Data URLs
                r'mailto:',       # Email URLs
            ]

            for pattern in invalid_patterns:
                if re.match(pattern, url, re.IGNORECASE):
                    return False

            # Check domain patterns - must look like a real domain
            domain_pattern = r'^[a-zA-Z0-9]([a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?(\.[a-zA-Z0-9]([a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?)*$'
            if not re.match(domain_pattern, domain):
                return False

            # Additional checks for media URLs
            # Must have a path that could contain media
            if not parsed.path or parsed.path == '/':
                # Some domains like imgur.com/abc are valid even without file extensions
                if domain not in ['i.imgur.com', 'imgur.com', 'i.redd.it', 'v.redd.it']:
                    return False

            return True

        except Exception:
            # If URL parsing fails, consider it invalid
            return False

    def _download_with_retry(self, url: str, save_path: str,
                           progress_callback: Optional[Callable[[int, int], None]] = None) -> DownloadResult:
        """
        Internal method that handles the actual HTTP download with retry logic.

        This method is decorated with retry strategies for intelligent error handling.
        """
        start_time = time.time()

        # Respect rate limiting
        self._respect_rate_limit()

        # Make initial request to get headers
        with self._session.get(
            url,
            stream=True,
            timeout=(5.0, self.config.timeout_seconds),
            allow_redirects=True
        ) as response:
            response.raise_for_status()

            # Get file information
            total_size = int(response.headers.get('content-length', 0))
            filename = os.path.basename(save_path)

            # Fix filename extension if needed
            filename = self._fix_filename_extension(filename, response.headers)
            fixed_save_path = os.path.join(os.path.dirname(save_path), filename)

            # Check file size limits
            if total_size > self.config.max_file_size:
                return DownloadResult(
                    status=DownloadStatus.FAILED,
                    error_message=f"File too large: {total_size} bytes (limit: {self.config.max_file_size})"
                )

            # Check disk space availability
            if not self._check_disk_space(total_size, fixed_save_path):
                return DownloadResult(
                    status=DownloadStatus.FAILED,
                    error_message="Insufficient disk space for download"
                )

            # Create directory if needed
            os.makedirs(os.path.dirname(fixed_save_path), exist_ok=True)

            # Download with streaming and integrity validation
            downloaded = 0

            # Use BLAKE3 if available (10x faster), fallback to SHA256
            if BLAKE3_AVAILABLE:
                hasher = blake3.blake3()
                hash_name = "BLAKE3"
            else:
                hasher = hashlib.sha256()
                hash_name = "SHA256"

            with open(fixed_save_path, 'wb') as file:
                for chunk in response.iter_content(chunk_size=DOWNLOAD_CHUNK_SIZE):
                    if chunk:
                        size = file.write(chunk)
                        downloaded += size

                        # Update hash for integrity validation
                        hasher.update(chunk)

                        # Call progress callback if provided
                        if progress_callback:
                            progress_callback(downloaded, total_size)

            # Validate file integrity after download
            integrity_result = self._validate_file_integrity(
                fixed_save_path, downloaded, total_size,
                hasher.hexdigest(), response.headers, hash_name
            )

            if not integrity_result.is_valid:
                # Remove corrupted file
                try:
                    os.remove(fixed_save_path)
                except OSError:
                    pass

                return DownloadResult(
                    status=DownloadStatus.FAILED,
                    error_message=f"File integrity validation failed: {integrity_result.error_message}"
                )

            # Validate content type to detect HTML responses (Reddit wrapper pages)
            content_type = response.headers.get('content-type', '').lower()
            if 'text/html' in content_type:
                # Remove HTML file since it's not the expected image
                try:
                    os.remove(fixed_save_path)
                except OSError:
                    pass

                # Get platform-specific error message
                platform = url_transformer.get_domain_info(url)
                if platform:
                    platform_msg = f"{platform} served an HTML viewer page instead of raw file"
                else:
                    platform_msg = f"Received HTML instead of expected content from {urlparse(url).netloc}"

                self._logger.warning(f"Received HTML instead of image from {url}. "
                                   f"Content-Type: {content_type}. {platform_msg}")

                return DownloadResult(
                    status=DownloadStatus.FAILED,
                    error_message=f"{platform_msg}. Content-Type: {content_type}"
                )

            # Additional validation: Check for image-like filenames getting HTML content
            expected_image_ext = os.path.splitext(save_path)[1].lower() in ['.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp', '.tiff']
            if expected_image_ext and 'image/' not in content_type and content_type != 'application/octet-stream':
                self._logger.warning(f"Expected image but got Content-Type: {content_type} for {url}")

            # Create metadata
            metadata = MediaMetadata(
                url=url,
                media_type=self._detect_media_type(response.headers),
                file_size=downloaded,
                format=self._get_file_extension_from_headers(response.headers)
            )

            download_time = time.time() - start_time

            # Report successful download to rate limiter
            service_name = self.config.name.lower()
            rate_limit_manager.report_response(service_name, 200)

            return DownloadResult(
                status=DownloadStatus.SUCCESS,
                local_path=fixed_save_path,
                metadata=metadata,
                bytes_downloaded=downloaded,
                download_time=download_time
            )

    def download_file(self, url: str, save_path: str,
                     progress_callback: Optional[Callable[[int, int], None]] = None) -> DownloadResult:
        """
        Download a file using streaming with progress tracking and retry logic.

        Args:
            url: URL to download from
            save_path: Local path to save the file
            progress_callback: Optional callback for progress updates (downloaded, total)

        Returns:
            DownloadResult with status and metadata
        """
        # Validate URL before attempting download
        if not self._validate_url(url):
            return DownloadResult(
                status=DownloadStatus.INVALID_URL,
                error_message=f"Invalid URL format or pattern: {url}"
            )

        try:
            # Apply retry logic to the download operation
            if TENACITY_AVAILABLE:
                retry_download = self._retry_decorator(self._download_with_retry)
                return retry_download(url, save_path, progress_callback)
            else:
                # Fallback without retry if tenacity not available
                return self._download_with_retry(url, save_path, progress_callback)

        except Timeout:
            # Report timeout as server error
            service_name = self.config.name.lower()
            rate_limit_manager.report_response(service_name, 408)  # Request Timeout

            return DownloadResult(
                status=DownloadStatus.FAILED,
                error_message=f"Download timed out after {self.config.timeout_seconds} seconds"
            )
        except ConnectionError as e:
            # Report connection error
            service_name = self.config.name.lower()
            rate_limit_manager.report_response(service_name, 503)  # Service Unavailable

            return DownloadResult(
                status=DownloadStatus.FAILED,
                error_message=f"Connection error: {str(e)}"
            )
        except RequestsError as e:
            service_name = self.config.name.lower()

            # Handle HTTP errors from both curl_cffi and standard requests
            if hasattr(e, 'response') and e.response is not None:
                status_code = e.response.status_code

                if status_code == 429:
                    # Extract retry-after header if available
                    retry_after = e.response.headers.get('retry-after')
                    retry_seconds = int(retry_after) if retry_after else 60

                    # Report rate limit to manager
                    rate_limit_manager.report_response(service_name, 429, retry_seconds)

                    return DownloadResult(
                        status=DownloadStatus.RATE_LIMITED,
                        error_message="Rate limit exceeded",
                        retry_after=retry_seconds
                    )
                else:
                    # Report HTTP error to rate limiter
                    rate_limit_manager.report_response(service_name, status_code)

                    return DownloadResult(
                        status=DownloadStatus.FAILED,
                        error_message=f"HTTP error {status_code}: {str(e)}"
                    )
            else:
                # Handle requests without response (connection errors, etc.)
                rate_limit_manager.report_response(service_name, 500)

                return DownloadResult(
                    status=DownloadStatus.FAILED,
                    error_message=f"Request error: {str(e)}"
                )
        except OSError as e:
            # Handle disk space and permission errors specifically
            service_name = self.config.name.lower()

            if e.errno == 28:  # ENOSPC - No space left on device
                rate_limit_manager.report_response(service_name, 507)  # Insufficient Storage
                return DownloadResult(
                    status=DownloadStatus.FAILED,
                    error_message="No space left on device"
                )
            elif e.errno == 13:  # EACCES - Permission denied
                rate_limit_manager.report_response(service_name, 403)  # Forbidden
                return DownloadResult(
                    status=DownloadStatus.FAILED,
                    error_message="Permission denied writing to file"
                )
            else:
                rate_limit_manager.report_response(service_name, 500)
                return DownloadResult(
                    status=DownloadStatus.FAILED,
                    error_message=f"File system error: {str(e)}"
                )
        except Exception as e:
            # Report general error
            service_name = self.config.name.lower()
            rate_limit_manager.report_response(service_name, 500)  # Internal Server Error

            return DownloadResult(
                status=DownloadStatus.FAILED,
                error_message=f"Unexpected error: {str(e)}"
            )

    def download(self, url: str, save_path: str) -> DownloadResult:
        """
        Download media from URL - interface method for MediaDownloadManager.

        This method provides the standard interface expected by MediaDownloadManager
        and delegates to the existing download_file() implementation.

        Args:
            url: URL to download from
            save_path: Local path to save the file

        Returns:
            DownloadResult with status and metadata
        """
        # Validate URL before attempting download
        if not self._validate_url(url):
            return DownloadResult(
                status=DownloadStatus.INVALID_URL,
                error_message=f"Invalid URL format or pattern: {url}"
            )

        return self.download_file(url, save_path)

    def _detect_media_type(self, headers: Dict[str, str]) -> MediaType:
        """Detect media type from HTTP headers."""
        content_type = headers.get('content-type', '').lower()

        if 'image' in content_type:
            return MediaType.IMAGE
        elif 'video' in content_type:
            return MediaType.VIDEO
        elif 'audio' in content_type:
            return MediaType.AUDIO
        else:
            return MediaType.UNKNOWN

    def _validate_file_integrity(self, file_path: str, actual_size: int,
                               expected_size: int, computed_checksum: str,
                               headers: Dict[str, str], hash_algorithm: str = "SHA256") -> FileIntegrityResult:
        """
        Validate file integrity using multiple validation layers.

        Implements FIVER-inspired validation:
        1. Size validation (Content-Length vs actual)
        2. Checksum validation (computed during download)
        3. Format-specific validation (if libraries available)

        Args:
            file_path: Path to the downloaded file
            actual_size: Actual bytes downloaded
            expected_size: Expected size from Content-Length header
            computed_checksum: BLAKE3 or SHA256 checksum computed during download
            headers: HTTP response headers
            hash_algorithm: Hash algorithm used ("BLAKE3" or "SHA256")

        Returns:
            FileIntegrityResult indicating validation status
        """
        try:
            # Layer 1: Size validation
            if expected_size > 0 and actual_size != expected_size:
                return FileIntegrityResult(
                    is_valid=False,
                    error_message=f"Size mismatch: expected {expected_size}, got {actual_size}",
                    file_size=actual_size,
                    checksum=computed_checksum
                )

            # Layer 2: File existence and basic size check
            if not os.path.exists(file_path):
                return FileIntegrityResult(
                    is_valid=False,
                    error_message="Downloaded file does not exist"
                )

            file_stat_size = os.path.getsize(file_path)
            if file_stat_size != actual_size:
                return FileIntegrityResult(
                    is_valid=False,
                    error_message=f"File size inconsistency: downloaded {actual_size}, on disk {file_stat_size}",
                    file_size=file_stat_size,
                    checksum=computed_checksum
                )

            # Layer 3: Format-specific validation for images
            content_type = headers.get('content-type', '').lower()
            if 'image' in content_type and PIL_AVAILABLE:
                try:
                    with Image.open(file_path) as img:
                        # Try to load the image to verify it's not corrupted
                        img.verify()

                        # Basic sanity checks
                        if img.size[0] <= 0 or img.size[1] <= 0:
                            return FileIntegrityResult(
                                is_valid=False,
                                error_message="Invalid image dimensions",
                                file_size=actual_size,
                                checksum=computed_checksum
                            )

                except Exception as e:
                    return FileIntegrityResult(
                        is_valid=False,
                        error_message=f"Image validation failed: {str(e)}",
                        file_size=actual_size,
                        checksum=computed_checksum
                    )

            # Layer 4: Minimum size check (avoid empty/truncated files)
            if actual_size < MIN_MEDIA_FILE_SIZE:  # Configurable minimum for media files
                return FileIntegrityResult(
                    is_valid=False,
                    error_message=f"File too small ({actual_size} bytes), likely corrupted",
                    file_size=actual_size,
                    checksum=computed_checksum
                )

            # All validations passed
            return FileIntegrityResult(
                is_valid=True,
                file_size=actual_size,
                checksum=computed_checksum
            )

        except Exception as e:
            return FileIntegrityResult(
                is_valid=False,
                error_message=f"Integrity validation error: {str(e)}",
                file_size=actual_size,
                checksum=computed_checksum
            )

    def close(self):
        """Close the HTTP session."""
        if self._session:
            self._session.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()