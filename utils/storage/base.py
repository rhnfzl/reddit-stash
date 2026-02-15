"""
Core abstractions for storage providers.

Defines the StorageProviderProtocol that all storage backends must implement,
along with frozen dataclass value types for file metadata and sync results.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional, Protocol, runtime_checkable


class StorageProvider(Enum):
    """Supported storage provider types."""
    NONE = "none"
    DROPBOX = "dropbox"
    S3 = "s3"


@dataclass(frozen=True)
class StorageFileInfo:
    """Metadata for a file stored in a remote provider."""
    remote_path: str
    content_hash: Optional[str] = None  # BLAKE3 hex digest
    size_bytes: int = 0
    last_modified: Optional[str] = None  # ISO 8601 timestamp


@dataclass(frozen=True)
class SyncResult:
    """Result of a sync (upload or download) operation."""
    uploaded: int = 0
    downloaded: int = 0
    skipped: int = 0
    failed: int = 0
    bytes_transferred: int = 0
    elapsed_seconds: float = 0.0
    errors: List[str] = field(default_factory=list)

    @property
    def total_processed(self) -> int:
        return self.uploaded + self.downloaded + self.skipped + self.failed

    @property
    def success_rate(self) -> float:
        total = self.uploaded + self.downloaded + self.failed
        if total == 0:
            return 1.0
        return (self.uploaded + self.downloaded) / total

    def summary(self) -> str:
        """Human-readable summary of the sync result."""
        parts = []
        if self.uploaded:
            parts.append(f"{self.uploaded} uploaded")
        if self.downloaded:
            parts.append(f"{self.downloaded} downloaded")
        if self.skipped:
            parts.append(f"{self.skipped} skipped")
        if self.failed:
            parts.append(f"{self.failed} failed")

        size_mb = self.bytes_transferred / (1024 * 1024)
        parts.append(f"{size_mb:.2f} MB")
        parts.append(f"{self.elapsed_seconds:.1f}s")

        return ", ".join(parts)


@runtime_checkable
class StorageProviderProtocol(Protocol):
    """Protocol that all storage backends must implement."""

    def connect(self) -> None:
        """Establish connection / refresh credentials."""
        ...

    def upload_file(self, local_path: str, remote_path: str) -> StorageFileInfo:
        """Upload a single file. Returns metadata of the uploaded file."""
        ...

    def download_file(self, remote_path: str, local_path: str) -> StorageFileInfo:
        """Download a single file. Returns metadata of the downloaded file."""
        ...

    def list_files(self, remote_directory: str) -> List[StorageFileInfo]:
        """List all files under a remote directory (recursive)."""
        ...

    def get_file_info(self, remote_path: str) -> Optional[StorageFileInfo]:
        """Get metadata for a single remote file, or None if not found."""
        ...

    def file_exists(self, remote_path: str) -> bool:
        """Check whether a remote file exists."""
        ...

    def upload_directory(self, local_directory: str, remote_directory: str,
                         check_type: str = "DIR") -> SyncResult:
        """Upload an entire local directory to remote storage."""
        ...

    def download_directory(self, remote_directory: str, local_directory: str,
                           check_type: str = "DIR") -> SyncResult:
        """Download an entire remote directory to local storage."""
        ...

    def get_provider_name(self) -> str:
        """Return the human-readable provider name."""
        ...
