"""
Service abstractions for Reddit Stash media downloads.

This module provides Protocol-based interfaces for media download services,
enabling clean separation of concerns and dependency injection patterns.
Uses modern Python patterns including Protocols, dataclasses with slots,
and the Result pattern for robust error handling.
"""

from typing import Dict, Any, Optional, List, Protocol, runtime_checkable
from dataclasses import dataclass
from enum import Enum
import time


class MediaType(Enum):
    """Types of media content."""
    IMAGE = "image"
    VIDEO = "video"
    AUDIO = "audio"
    ALBUM = "album"
    UNKNOWN = "unknown"


class DownloadStatus(Enum):
    """Status of a download operation."""
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"
    RATE_LIMITED = "rate_limited"
    NOT_FOUND = "not_found"
    INVALID_URL = "invalid_url"


@dataclass(frozen=True)
class MediaMetadata:
    """Metadata for a media item."""
    url: str
    media_type: MediaType
    file_size: Optional[int] = None
    width: Optional[int] = None
    height: Optional[int] = None
    duration: Optional[float] = None
    format: Optional[str] = None
    title: Optional[str] = None
    description: Optional[str] = None


@dataclass(frozen=True)
class DownloadResult:
    """
    Result of a download operation following the Result pattern.

    This immutable data structure encapsulates both success and failure
    states, making error handling explicit and predictable.
    """
    status: DownloadStatus
    local_path: Optional[str] = None
    metadata: Optional[MediaMetadata] = None
    error_message: Optional[str] = None
    retry_after: Optional[int] = None  # Seconds to wait before retry
    bytes_downloaded: int = 0
    download_time: float = 0.0

    @property
    def is_success(self) -> bool:
        """Check if the download was successful."""
        return self.status == DownloadStatus.SUCCESS

    @property
    def is_failure(self) -> bool:
        """Check if the download failed."""
        return not self.is_success

    @property
    def should_retry(self) -> bool:
        """Check if this download should be retried."""
        return self.status in [DownloadStatus.FAILED, DownloadStatus.RATE_LIMITED]


@dataclass(frozen=True)
class ServiceConfig:
    """Configuration for a media service."""
    name: str
    enabled: bool = True
    api_keys: Optional[Dict[str, str]] = None
    rate_limit_per_minute: int = 60
    timeout_seconds: int = 15
    max_file_size: int = 52428800  # 50MB default
    user_agent: str = "Reddit Stash Media Downloader"
    # Security enhancements (2024-2025 best practices)
    max_redirects: int = 5
    connect_timeout: float = 5.0
    read_timeout: float = 15.0
    allowed_content_types: Optional[List[str]] = None  # None = allow all, [] = none, [list] = specific types
    verify_ssl: bool = True


@runtime_checkable
class MediaDownloaderProtocol(Protocol):
    """Protocol for media download services using structural subtyping."""

    def can_handle(self, url: str) -> bool:
        """
        Check if this service can handle the given URL.

        Args:
            url: The URL to check

        Returns:
            True if this service can handle the URL
        """
        ...

    def get_metadata(self, url: str) -> Optional[MediaMetadata]:
        """
        Get metadata for a media item without downloading.

        Args:
            url: The URL to analyze

        Returns:
            MediaMetadata if successful, None if failed
        """
        ...

    def download(self, url: str, save_path: str) -> DownloadResult:
        """
        Download media from the given URL.

        Args:
            url: The URL to download from
            save_path: Local path to save the file

        Returns:
            DownloadResult with status and details
        """
        ...

    def get_service_name(self) -> str:
        """Get the name of this service."""
        ...

    def is_rate_limited(self) -> bool:
        """Check if the service is currently rate limited."""
        ...

    def get_rate_limit_reset_time(self) -> Optional[float]:
        """Get the time when rate limit resets (Unix timestamp)."""
        ...


@runtime_checkable
class ContentRecoveryProtocol(Protocol):
    """Protocol for content recovery services using structural subtyping."""

    def can_recover(self, url: str) -> bool:
        """
        Check if this recovery service can attempt to recover the URL.

        Args:
            url: The URL to check

        Returns:
            True if recovery is possible
        """
        ...

    def recover_content(self, url: str) -> Optional[str]:
        """
        Attempt to recover content from the URL.

        Args:
            url: The original URL that's no longer accessible

        Returns:
            New working URL if recovery successful, None otherwise
        """
        ...

    def get_service_name(self) -> str:
        """Get the name of this recovery service."""
        ...


@runtime_checkable
class RateLimiterProtocol(Protocol):
    """Protocol for rate limiting services using structural subtyping."""

    def can_proceed(self, service_name: str) -> bool:
        """Check if a request can proceed for the given service."""
        ...

    def record_request(self, service_name: str, success: bool = True) -> None:
        """Record a request for rate limiting purposes."""
        ...

    def get_wait_time(self, service_name: str) -> float:
        """Get the time to wait before the next request."""
        ...

    def handle_rate_limit_response(self, service_name: str, retry_after: Optional[int] = None) -> None:
        """Handle a rate limit response from a service."""
        ...


@runtime_checkable
class RetryQueueProtocol(Protocol):
    """Protocol for retry queue management using structural subtyping."""

    def add_failed_download(self, url: str, error: str, service_name: str) -> None:
        """Add a failed download to the retry queue."""
        ...

    def get_pending_retries(self, service_name: Optional[str] = None) -> List[Dict[str, Any]]:
        """Get pending retry items, optionally filtered by service."""
        ...

    def mark_retry_completed(self, url: str, success: bool) -> None:
        """Mark a retry as completed."""
        ...

    def cleanup_expired_retries(self) -> int:
        """Remove expired retry items and return count removed."""
        ...


class BaseMediaDownloader:
    """
    Base class for media downloaders with common functionality.

    Implements MediaDownloaderProtocol through structural subtyping,
    providing common rate limiting and error handling functionality.
    """

    def __init__(self, config: ServiceConfig):
        self.config = config
        self._last_request_time = 0.0
        self._rate_limit_reset_time = None
        self._rate_limited = False

    def _can_make_request(self) -> bool:
        """Check if we can make a request based on rate limiting."""
        current_time = time.time()

        # Check if rate limit has expired
        if self._rate_limited and self._rate_limit_reset_time:
            if current_time >= self._rate_limit_reset_time:
                self._rate_limited = False
                self._rate_limit_reset_time = None

        if self._rate_limited:
            return False

        # Check minimum time between requests
        min_interval = 60.0 / self.config.rate_limit_per_minute
        time_since_last = current_time - self._last_request_time

        return time_since_last >= min_interval

    def _record_request(self) -> None:
        """Record that a request was made."""
        self._last_request_time = time.time()

    def _handle_rate_limit(self, retry_after: Optional[int] = None) -> None:
        """Handle a rate limit response."""
        self._rate_limited = True
        if retry_after:
            self._rate_limit_reset_time = time.time() + retry_after
        else:
            # Default rate limit duration
            self._rate_limit_reset_time = time.time() + 300  # 5 minutes

    def is_rate_limited(self) -> bool:
        """Check if the service is currently rate limited."""
        if self._rate_limited and self._rate_limit_reset_time:
            if time.time() >= self._rate_limit_reset_time:
                self._rate_limited = False
                self._rate_limit_reset_time = None
        return self._rate_limited

    def get_rate_limit_reset_time(self) -> Optional[float]:
        """Get the time when rate limit resets."""
        return self._rate_limit_reset_time

    def _validate_file_size(self, file_size: int) -> bool:
        """Check if file size is within limits."""
        return file_size <= self.config.max_file_size

    def _create_error_result(self, error_msg: str, status: DownloadStatus = DownloadStatus.FAILED) -> DownloadResult:
        """Create a standardized error result."""
        return DownloadResult(
            status=status,
            error_message=error_msg,
            retry_after=None
        )

    def _create_success_result(self, local_path: str, metadata: MediaMetadata,
                              bytes_downloaded: int, download_time: float) -> DownloadResult:
        """Create a standardized success result."""
        return DownloadResult(
            status=DownloadStatus.SUCCESS,
            local_path=local_path,
            metadata=metadata,
            bytes_downloaded=bytes_downloaded,
            download_time=download_time
        )


class ServiceRegistry:
    """
    Registry for managing media download services using Protocol-based dependency injection.

    This registry follows the dependency injection container pattern, managing
    service instances and their dependencies in a type-safe manner using Protocols.
    """

    def __init__(self):
        self._downloaders: List[MediaDownloaderProtocol] = []
        self._recovery_services: List[ContentRecoveryProtocol] = []
        self._rate_limiter: Optional[RateLimiterProtocol] = None
        self._retry_queue: Optional[RetryQueueProtocol] = None

    def register_downloader(self, downloader: MediaDownloaderProtocol) -> None:
        """Register a media downloader service."""
        # Verify the service implements the protocol at runtime
        if not isinstance(downloader, MediaDownloaderProtocol):
            raise TypeError("Service must implement MediaDownloaderProtocol")
        self._downloaders.append(downloader)

    def register_recovery_service(self, recovery: ContentRecoveryProtocol) -> None:
        """Register a content recovery service."""
        if not isinstance(recovery, ContentRecoveryProtocol):
            raise TypeError("Service must implement ContentRecoveryProtocol")
        self._recovery_services.append(recovery)

    def set_rate_limiter(self, rate_limiter: RateLimiterProtocol) -> None:
        """Set the rate limiter."""
        if not isinstance(rate_limiter, RateLimiterProtocol):
            raise TypeError("Service must implement RateLimiterProtocol")
        self._rate_limiter = rate_limiter

    def set_retry_queue(self, retry_queue: RetryQueueProtocol) -> None:
        """Set the retry queue."""
        if not isinstance(retry_queue, RetryQueueProtocol):
            raise TypeError("Service must implement RetryQueueProtocol")
        self._retry_queue = retry_queue

    def get_downloader_for_url(self, url: str) -> Optional[MediaDownloaderProtocol]:
        """Get the appropriate downloader for a URL."""
        for downloader in self._downloaders:
            try:
                if downloader.can_handle(url):
                    return downloader
            except Exception:
                # Skip downloaders that fail the can_handle check
                continue
        return None

    def get_recovery_services_for_url(self, url: str) -> List[ContentRecoveryProtocol]:
        """Get recovery services that can handle a URL."""
        return [service for service in self._recovery_services
                if service.can_recover(url)]

    def get_rate_limiter(self) -> Optional[RateLimiterProtocol]:
        """Get the rate limiter."""
        return self._rate_limiter

    def get_retry_queue(self) -> Optional[RetryQueueProtocol]:
        """Get the retry queue."""
        return self._retry_queue

    def get_all_downloaders(self) -> List[MediaDownloaderProtocol]:
        """Get all registered downloaders."""
        return self._downloaders.copy()

    def get_service_status(self) -> Dict[str, Any]:
        """Get status of all registered services."""
        status = {
            'downloaders': [],
            'recovery_services': [],
            'rate_limiter_enabled': self._rate_limiter is not None,
            'retry_queue_enabled': self._retry_queue is not None
        }

        for downloader in self._downloaders:
            status['downloaders'].append({
                'name': downloader.get_service_name(),
                'rate_limited': downloader.is_rate_limited(),
                'reset_time': downloader.get_rate_limit_reset_time()
            })

        for recovery in self._recovery_services:
            status['recovery_services'].append({
                'name': recovery.get_service_name()
            })

        return status


# Global service registry instance
_service_registry = None

def get_service_registry() -> ServiceRegistry:
    """Get the global service registry instance."""
    global _service_registry
    if _service_registry is None:
        _service_registry = ServiceRegistry()
    return _service_registry