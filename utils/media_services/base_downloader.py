"""
Streaming HTTP downloader shared by the media services.
"""

import errno
import os
import time
import re
import shutil
import logging
import threading
from typing import Any
from collections.abc import Callable
from urllib.parse import urlparse

import blake3
from PIL import Image
from tenacity import (
    retry, stop_after_attempt, wait_exponential,
    retry_if_exception, before_sleep_log
)

from utils.media_services.media_types import (
    DownloadResult, DownloadStatus,
    MediaMetadata, MediaType, ServiceConfig
)
from utils.rate_limiter import RateLimitTimeout, parse_retry_after, rate_limit_manager
from utils.media_services.url_transformer import url_transformer
from utils.constants import (
    DOWNLOAD_CHUNK_SIZE, DISK_SPACE_SAFETY_FACTOR, IMAGE_URL_EXTENSIONS, MAX_URL_LENGTH, MIN_MEDIA_FILE_SIZE,
    MIN_URL_LENGTH, TRUSTED_FILE_EXTENSIONS,
)
from utils.media_services.safe_http import (
    CURL_CFFI_AVAILABLE, ConnectionError, HTTPAdapter, RequestsError, Timeout,
    RedirectFaultError, UnsafeRedirectError, get_with_safe_redirects, requests,
)

# Fans out to unrelated hosts, so one host's trouble says nothing about the next one.
UNSHARED_SERVICE = 'generic'

_DOMAIN_PATTERN = re.compile(
    r'^[a-zA-Z0-9]([a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?(\.[a-zA-Z0-9]([a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?)*$'
)
# Only these media hosts may have an empty path
_EMPTY_PATH_MEDIA_HOSTS = frozenset({'i.imgur.com', 'imgur.com', 'i.redd.it', 'v.redd.it'})


def should_retry_download_error(exception: BaseException) -> bool:
    """
    Determine if we should retry based on the exception type.

    Retry strategy:
    - Connection errors: Yes (network issues)
    - Timeouts: Yes (temporary server issues)
    - Other request errors with no HTTP response: Yes (transport failures)
    - Server errors (5xx): Yes (temporary server issues)
    - Rate limits (429): No (handled by rate limiter)
    - Client errors (4xx): No (permanent failures)
    """
    if isinstance(exception, (ConnectionError, Timeout)):
        return True

    if not isinstance(exception, RequestsError):
        return False

    response = getattr(exception, 'response', None)
    return response is None or 500 <= response.status_code < 600


def create_download_retry_decorator() -> Callable[[Callable[..., DownloadResult]], Callable[..., DownloadResult]]:
    """Create the retry decorator used for media downloads."""
    return retry(
        # Retry conditions
        retry=retry_if_exception(should_retry_download_error),

        # Stop after 3 attempts for most errors
        stop=stop_after_attempt(3),

        # Exponential backoff: wait 1s, 2s, 4s between retries
        wait=wait_exponential(multiplier=1, min=1, max=10),

        before_sleep=before_sleep_log(logging.getLogger(__name__), logging.WARNING),

        # Let the final error reach download_file's handlers, which classify and report it
        reraise=True
    )


class BaseHTTPDownloader:
    """
    Base class for HTTP-based media downloaders with streaming support.

    Owns the pooled HTTP session, connect and read timeouts, and retries with
    exponential backoff.
    """

    def __init__(self, config: ServiceConfig):
        self.config = config
        self._session = None
        self._setup_session()

        self._retry_decorator = create_download_retry_decorator()

        self._logger = logging.getLogger(f"{__name__}.{self.__class__.__name__}")

    def _setup_session(self):
        """Setup HTTP session with browser impersonation."""
        if CURL_CFFI_AVAILABLE:
            # Use curl_cffi with Chrome browser impersonation for better TLS fingerprinting
            self._session = requests.Session(
                impersonate="chrome110",  # Impersonate Chrome 110 for optimal compatibility
                timeout=(self.config.connect_timeout, self.config.read_timeout),
                max_redirects=self.config.max_redirects,
                verify=self.config.verify_ssl
            )
        else:
            # Fallback to standard requests session
            self._session = requests.Session()

            # Configure security settings
            self._session.max_redirects = self.config.max_redirects
            self._session.verify = self.config.verify_ssl

            # No adapter retries: download_file's retry policy is the only one
            adapter = HTTPAdapter(
                pool_connections=10,
                pool_maxsize=20
            )

            self._session.mount("http://", adapter)
            self._session.mount("https://", adapter)

        # DNS pinning is ineffective if a proxy from the environment resolves the
        # request elsewhere. Downloads must connect to the validated destination.
        self._session.trust_env = False

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

        # A longer wait, such as Imgur's backoff, ends this item as RATE_LIMITED rather than hold the worker.
        if not rate_limit_manager.acquire(service_name, timeout=90):
            raise RateLimitTimeout(f"Rate limit timeout for service '{service_name}'")

    def _get_file_extension_from_headers(self, headers: dict[str, str]) -> str | None:
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

    def _fix_filename_extension(self, filename: str, headers: dict[str, str]) -> str:
        """Fix filename extension based on Content-Type if needed."""
        # If filename already has a valid extension, keep it
        name, ext = os.path.splitext(filename)
        if ext.lower() in TRUSTED_FILE_EXTENSIONS:
            return filename

        # Try to get extension from headers
        header_ext = self._get_file_extension_from_headers(headers)
        if header_ext:
            return f"{name}{header_ext}"

        return filename

    def _validate_content_type(self, content_type: str) -> bool:
        """
        Validate content type against allowed types for security.

        Args:
            content_type: The content-type header value

        Returns:
            True if content type is allowed, False otherwise
        """
        if self.config.allowed_content_types is None:
            # None means allow all content types
            return True

        if not self.config.allowed_content_types:
            # Empty list means no content types allowed
            return False

        # Normalize content type (remove charset, etc.)
        content_type = content_type.lower().split(';')[0].strip()

        # Check against allowed types (supports wildcards like image/*)
        for allowed_type in self.config.allowed_content_types:
            allowed_type = allowed_type.lower().strip()

            if allowed_type == content_type:
                return True

            # Support wildcard matching (e.g., image/* matches image/jpeg)
            if allowed_type.endswith('/*'):
                category = allowed_type[:-2]
                if content_type.startswith(category + '/'):
                    return True

        return False

    def _error_message_with_context(self, base_message: str, url: str | None = None,
                                    context: dict[str, Any] | None = None) -> str:
        """
        Append service, URL and debugging context to an error message.

        Args:
            base_message: Base error message
            url: URL being processed (if applicable)
            context: Additional context for debugging

        Returns:
            The message with its context appended
        """
        parts = [base_message]

        parts.append(f"[Service: {self.config.name}]")

        # Add URL context (truncated for readability)
        if url:
            display_url = url if len(url) <= 100 else url[:97] + "..."
            parts.append(f"[URL: {display_url}]")

        if context:
            context_parts = []
            for key, value in context.items():
                if value is not None:
                    context_parts.append(f"{key}={value}")
            if context_parts:
                parts.append(f"[Context: {', '.join(context_parts)}]")

        return " ".join(parts)

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

        url = url.strip()
        if not MIN_URL_LENGTH <= len(url) <= MAX_URL_LENGTH:
            return False

        try:
            parsed = urlparse(url)
            domain = self._domain_without_port(parsed.netloc, parsed.port)
        except ValueError:  # malformed URL or port
            return False

        if parsed.scheme.lower() not in ['http', 'https'] or not self._looks_like_domain(domain):
            return False

        # Must have a path that could contain media
        return bool(parsed.path and parsed.path != '/') or domain in _EMPTY_PATH_MEDIA_HOSTS

    @staticmethod
    def _domain_without_port(netloc: str, port: int | None) -> str:
        """Lowercase a netloc and drop its port. Credentials stay, and fail the domain pattern."""
        domain = netloc.lower()
        return domain if port is None else domain.rsplit(':', 1)[0]

    @staticmethod
    def _looks_like_domain(domain: str) -> bool:
        """A domain must contain at least one dot (except localhost) and match the domain pattern."""
        return ('.' in domain or domain == 'localhost') and bool(_DOMAIN_PATTERN.match(domain))

    def _get_with_safe_redirects(
        self,
        url: str,
        request_kwargs: dict[str, Any],
        method: str = "get",
    ) -> tuple[Any, str]:
        """Fetch a URL through this downloader's session, validating every redirect target."""
        return get_with_safe_redirects(self._session, url, request_kwargs, self.config.max_redirects, method)

    def _discard(self, path: str) -> None:
        """Remove an unusable download. Its result already reports the failure."""
        try:
            os.remove(path)
        except OSError as e:
            self._logger.warning(f"Could not remove unusable download {path}: {e}")

    def _download_with_retry(self, url: str, save_path: str,
                             extra_headers: dict[str, str] | None = None) -> DownloadResult:
        """One download attempt. download_file wraps this in the retry policy on each call."""
        start_time = time.time()

        # Respect rate limiting
        self._respect_rate_limit()

        # Make initial request to get headers
        request_kwargs = dict(
            stream=True,
            timeout=(self.config.connect_timeout, self.config.read_timeout),
            allow_redirects=False,
        )
        if extra_headers:
            request_kwargs['headers'] = extra_headers

        response, final_url = self._get_with_safe_redirects(url, request_kwargs)
        with response:
            response.raise_for_status()

            rejection = self._reject_content_type(url, save_path, response.headers)
            if rejection:
                return rejection

            total_size = int(response.headers.get('content-length', 0))
            filename = self._fix_filename_extension(os.path.basename(save_path), response.headers)
            fixed_save_path = os.path.join(os.path.dirname(save_path), filename)

            rejection = self._reject_before_writing(url, total_size, fixed_save_path)
            if rejection:
                return rejection

            os.makedirs(os.path.dirname(fixed_save_path), exist_ok=True)

            # A failed download must not cost a file already saved at this path.
            # The leading dot keeps a crash's leftover out of uploads, as they skip dotfiles.
            temp_path = os.path.join(os.path.dirname(fixed_save_path), f".{filename}.{threading.get_ident()}.part")
            try:
                downloaded, checksum = self._stream_to_file(response, temp_path)

                failure = self._written_file_failure(
                    url, temp_path, downloaded, total_size, checksum, response.headers
                )
                if failure:
                    return failure

                os.replace(temp_path, fixed_save_path)
            finally:
                if os.path.exists(temp_path):
                    self._discard(temp_path)

            metadata = MediaMetadata(
                url=final_url,
                media_type=self._detect_media_type(response.headers),
                file_size=downloaded,
                format=self._get_file_extension_from_headers(response.headers)
            )

            # Report successful download to rate limiter
            rate_limit_manager.report_response(self.config.name.lower(), 200)

            return DownloadResult(
                status=DownloadStatus.SUCCESS,
                local_path=fixed_save_path,
                content_hash=checksum,
                metadata=metadata,
                bytes_downloaded=downloaded,
                download_time=time.time() - start_time
            )

    def _reject_content_type(self, url: str, save_path: str, headers: dict[str, str]) -> DownloadResult | None:
        """Return a rejection if the declared content type should not be saved."""
        content_type = headers.get('content-type', '').lower()

        if not self._validate_content_type(content_type):
            return DownloadResult(
                status=DownloadStatus.REJECTED,
                error_message=self._error_message_with_context(
                    f"Content type '{content_type}' not allowed",
                    url=url,
                    context={
                        'allowed_types': str(self.config.allowed_content_types),
                        'response_headers': 'content-type'
                    }
                )
            )

        # HTML wrapper pages (Reddit, Imgur viewers) instead of the file itself
        if 'text/html' in content_type:
            platform = url_transformer.get_domain_info(url)
            if platform:
                platform_msg = f"{platform} served an HTML viewer page instead of raw file"
            else:
                platform_msg = f"Received HTML instead of expected content from {urlparse(url).netloc}"

            self._logger.warning(f"Received HTML instead of image from {url}. "
                                 f"Content-Type: {content_type}. {platform_msg}")

            return DownloadResult(
                status=DownloadStatus.REJECTED,
                error_message=f"{platform_msg}. Content-Type: {content_type}"
            )

        if (os.path.splitext(save_path)[1].lower() in IMAGE_URL_EXTENSIONS
                and 'image/' not in content_type and content_type != 'application/octet-stream'):
            self._logger.warning(f"Expected image but got Content-Type: {content_type} for {url}")

        return None

    def _reject_before_writing(self, url: str, total_size: int, path: str) -> DownloadResult | None:
        """Return a result if the declared size rules out saving this file."""
        if total_size > self.config.max_file_size:
            return DownloadResult(
                status=DownloadStatus.REJECTED,
                error_message=self._error_message_with_context(
                    f"File too large: {total_size:,} bytes",
                    url=url,
                    context={
                        'limit_bytes': f"{self.config.max_file_size:,}",
                        'content_length': str(total_size)
                    }
                )
            )

        if not self._check_disk_space(total_size, path):
            return DownloadResult(
                status=DownloadStatus.FAILED,
                error_message=self._error_message_with_context(
                    "Insufficient disk space for download",
                    url=url,
                    context={
                        'file_size_mb': f"{total_size / (1024**2):.1f}",
                        'safety_factor': str(DISK_SPACE_SAFETY_FACTOR),
                        'save_path': os.path.dirname(path)
                    }
                )
            )

        return None

    def _stream_to_file(self, response: Any, path: str) -> tuple[int, str]:
        """Write the body to path, stopping once it passes max_file_size. Returns (bytes written, hash)."""
        downloaded = 0
        hasher = blake3.blake3()

        with open(path, 'wb') as file:
            for chunk in response.iter_content(chunk_size=DOWNLOAD_CHUNK_SIZE):
                if chunk:
                    downloaded += file.write(chunk)

                    # Streaming size enforcement when Content-Length was missing
                    if self.config.max_file_size and downloaded > self.config.max_file_size:
                        break

                    hasher.update(chunk)

        return downloaded, hasher.hexdigest()

    def _written_file_failure(self, url: str, path: str, downloaded: int, total_size: int,
                            checksum: str, headers: dict[str, str]) -> DownloadResult | None:
        """Return a failure if the file just written must not be kept."""
        if self.config.max_file_size and downloaded > self.config.max_file_size:
            return DownloadResult(
                status=DownloadStatus.REJECTED,
                error_message=self._error_message_with_context(
                    f"Download aborted: exceeded max size {self.config.max_file_size:,} bytes during streaming",
                    url=url,
                    context={
                        'downloaded_bytes': str(downloaded),
                        'max_file_size': str(self.config.max_file_size)
                    }
                )
            )

        problem = self._find_integrity_problem(path, downloaded, total_size, headers)
        if problem:
            return DownloadResult(
                status=DownloadStatus.FAILED,
                error_message=self._error_message_with_context(
                    f"File integrity validation failed: {problem}",
                    url=url,
                    context={
                        'downloaded_bytes': str(downloaded),
                        'expected_bytes': str(total_size),
                        'hash_algorithm': 'BLAKE3',
                        'checksum': checksum[:16] + "..."
                    }
                )
            )

        return None

    def download_file(self, url: str, save_path: str,
                      extra_headers: dict[str, str] | None = None) -> DownloadResult:
        """
        Download a file using streaming and retry logic.

        Args:
            url: URL to download from
            save_path: Local path to save the file
            extra_headers: Optional per-request headers (merged with session headers, not mutating session)

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
            retry_download = self._retry_decorator(self._download_with_retry)
            return retry_download(url, save_path, extra_headers)

        except UnsafeRedirectError as e:
            return DownloadResult(
                status=DownloadStatus.INVALID_URL,
                error_message=str(e),
            )
        except RedirectFaultError as e:
            return DownloadResult(
                status=DownloadStatus.REJECTED,
                error_message=str(e),
            )
        except RateLimitTimeout as e:
            return DownloadResult(
                status=DownloadStatus.RATE_LIMITED,
                error_message=self._error_message_with_context(str(e), url=url),
            )
        except Timeout as e:
            return self._failed(
                url, "Download timed out", 408,
                connect_timeout=f"{self.config.connect_timeout}s",
                read_timeout=f"{self.config.read_timeout}s",
                timeout_type=type(e).__name__,
            )
        except ConnectionError as e:
            return self._failed(
                url, "Connection failed", 503,
                error_type=type(e).__name__,
                details=str(e)[:100],  # Truncate long error messages
                ssl_verify=self.config.verify_ssl,
            )
        except RequestsError as e:
            return self._result_for_request_error(url, e)
        except OSError as e:
            return self._result_for_os_error(url, save_path, e)
        except Exception as e:
            return self._failed(
                url, "Unexpected error during download", 500,
                error_type=type(e).__name__,
                details=str(e)[:100],
                save_path=save_path,
            )

    def _failed(self, url: str, message: str, reported_status: int, **context: Any) -> DownloadResult:
        """Report a failure to the rate limiter and build its result."""
        rate_limit_manager.report_response(self.config.name.lower(), reported_status)
        return DownloadResult(
            status=DownloadStatus.FAILED,
            error_message=self._error_message_with_context(message, url=url, context=context),
        )

    def _result_for_request_error(self, url: str, error: RequestsError) -> DownloadResult:
        """Classify an HTTP error from curl_cffi or standard requests."""
        response = getattr(error, 'response', None)
        if response is None:
            # Requests without a response (connection errors, etc.)
            return self._failed(
                url, "Request error", 500,
                error_type=type(error).__name__,
                details=str(error)[:100],
                has_response='false',
            )

        service_name = self.config.name.lower()
        status_code = response.status_code

        if status_code == 429:
            retry_after = parse_retry_after(response)

            # One host's limit must not stall other hosts
            if service_name != UNSHARED_SERVICE:
                # Without a header the service's own backoff policy applies
                rate_limit_manager.report_response(service_name, 429, retry_after)

            return DownloadResult(
                status=DownloadStatus.RATE_LIMITED,
                error_message=self._error_message_with_context(
                    "Rate limit exceeded",
                    url=url,
                    context={
                        'retry_after_seconds': str(retry_after),
                        'status_code': str(status_code),
                        'has_retry_after_header': str(retry_after is not None)
                    }
                ),
                retry_after=retry_after
            )

        rate_limit_manager.report_response(service_name, status_code)

        return DownloadResult(
            status={
                403: DownloadStatus.FORBIDDEN,
                404: DownloadStatus.NOT_FOUND,
            }.get(status_code, DownloadStatus.FAILED),
            error_message=self._error_message_with_context(
                f"HTTP error {status_code}",
                url=url,
                context={
                    'status_code': str(status_code),
                    'details': str(error)[:100]
                }
            )
        )

    def _result_for_os_error(self, url: str, save_path: str, error: OSError) -> DownloadResult:
        """Classify a disk or permission error."""
        if error.errno == errno.ENOSPC:
            return self._failed(
                url, "No space left on device", 507,
                errno=error.errno, save_path=save_path, error_details=error,
            )
        if error.errno == errno.EACCES:
            return self._failed(
                url, "Permission denied writing to file", 403,
                errno=error.errno, save_path=save_path, directory=os.path.dirname(save_path),
            )
        return self._failed(
            url, "File system error", 500,
            errno=error.errno,
            error_type=type(error).__name__,
            save_path=save_path,
            details=str(error)[:100],
        )

    def download(self, url: str, save_path: str) -> DownloadResult:
        """Entry point for MediaDownloadManager; subclasses override it to route by media type."""
        return self.download_file(url, save_path)

    def _detect_media_type(self, headers: dict[str, str]) -> MediaType:
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

    def _find_integrity_problem(self, file_path: str, actual_size: int,
                                expected_size: int, headers: dict[str, str]) -> str | None:
        """
        Return why a finished download is unusable, or None if it looks intact.

        Checks the size against Content-Length and the file on disk, images by
        decoding them, and everything against a minimum size.

        Args:
            file_path: Path to the downloaded file
            actual_size: Actual bytes downloaded
            expected_size: Expected size from Content-Length header
            headers: HTTP response headers
        """
        try:
            if expected_size > 0 and actual_size != expected_size:
                return f"Size mismatch: expected {expected_size}, got {actual_size}"

            if not os.path.exists(file_path):
                return "Downloaded file does not exist"

            file_stat_size = os.path.getsize(file_path)
            if file_stat_size != actual_size:
                return f"File size inconsistency: downloaded {actual_size}, on disk {file_stat_size}"

            if 'image' in headers.get('content-type', '').lower():
                problem = self._find_image_problem(file_path)
                if problem:
                    return problem

            # Avoid empty/truncated files
            if actual_size < MIN_MEDIA_FILE_SIZE:
                return f"File too small ({actual_size} bytes), likely corrupted"

            return None

        except Exception as e:
            return f"Integrity validation error: {str(e)}"

    @staticmethod
    def _find_image_problem(file_path: str) -> str | None:
        """Return why an image file cannot be decoded, or None if it can."""
        try:
            with Image.open(file_path) as img:
                img.verify()
                if img.size[0] <= 0 or img.size[1] <= 0:
                    return "Invalid image dimensions"
        except Exception as e:
            return f"Image validation failed: {str(e)}"
        return None

    def close(self):
        """Close the HTTP session."""
        if self._session:
            self._session.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
