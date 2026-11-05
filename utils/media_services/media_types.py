"""Value types shared by the media download services."""

from dataclasses import dataclass
from enum import Enum


class MediaType(Enum):
    """Types of media content."""
    IMAGE = "image"
    VIDEO = "video"
    AUDIO = "audio"
    ALBUM = "album"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class GalleryImage:
    """One image of a Reddit gallery post."""
    url: str
    gallery_id: str


class DownloadStatus(Enum):
    """Status of a download operation."""
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"
    RATE_LIMITED = "rate_limited"
    NOT_FOUND = "not_found"
    FORBIDDEN = "forbidden"
    INVALID_URL = "invalid_url"
    REJECTED = "rejected"  # The service answered but this file is unusable; may be retried later


# These say something about the service itself, so they count against its breaker.
SERVICE_FAILURE_STATUSES = frozenset({DownloadStatus.FAILED, DownloadStatus.RATE_LIMITED})

# A later run cannot help: the file is gone or the URL is refused.
NEVER_RETRY_STATUSES = frozenset({DownloadStatus.NOT_FOUND, DownloadStatus.INVALID_URL})


@dataclass(frozen=True)
class MediaMetadata:
    """Metadata for a media item."""
    url: str
    media_type: MediaType
    file_size: int | None = None
    width: int | None = None
    height: int | None = None
    duration: float | None = None
    format: str | None = None
    title: str | None = None
    description: str | None = None


@dataclass(frozen=True)
class DownloadResult:
    """
    Result of a download operation following the Result pattern.

    This immutable data structure encapsulates both success and failure
    states, making error handling explicit and predictable.
    """
    status: DownloadStatus
    local_path: str | None = None
    content_hash: str | None = None
    metadata: MediaMetadata | None = None
    error_message: str | None = None
    retry_after: int | None = None  # Seconds to wait before retry
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
        """Check if this download should be queued for a later run."""
        return (self.is_failure and self.status != DownloadStatus.SKIPPED
                and self.status not in NEVER_RETRY_STATUSES)


@dataclass(frozen=True)
class ServiceConfig:
    """Configuration for a media service."""
    name: str
    enabled: bool = True
    timeout_seconds: int = 15
    max_file_size: int = 52428800  # 50MB default
    user_agent: str = "Reddit Stash Media Downloader"
    max_redirects: int = 5
    connect_timeout: float = 5.0
    read_timeout: float = 15.0
    allowed_content_types: list[str] | None = None  # None = allow all, [] = none, [list] = specific types
    verify_ssl: bool = True
