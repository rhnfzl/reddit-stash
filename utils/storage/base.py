"""
Core abstractions for storage providers.

Defines the StorageProviderProtocol that all storage backends must implement,
along with frozen dataclass value types for file metadata and sync results.
"""

import functools
import logging
import os
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Literal, Protocol, TypeVar, runtime_checkable
from collections.abc import Callable, Iterable, Iterator

from utils.constants import BYTES_PER_MB, LOG_FILE_NAME

logger = logging.getLogger(__name__)

MovedField = Literal["uploaded", "downloaded"]
FileStatus = Literal["uploaded", "downloaded", "skipped", "failed"]
FileOutcome = tuple[FileStatus, int, str | None]  # SyncTally.record arguments

_T = TypeVar("_T")


class StorageError(RuntimeError):
    """A provider call failed; __cause__ holds the SDK error. download_file raises it for a missing object too."""


def raises_storage_error(method: Callable[..., _T]) -> Callable[..., _T]:
    """Report any failure of a provider call as StorageError, keeping the message."""
    @functools.wraps(method)
    def wrapper(*args, **kwargs):
        try:
            return method(*args, **kwargs)
        except StorageError:
            raise
        except Exception as exc:
            raise StorageError(str(exc)) from exc
    return wrapper


class StorageProvider(Enum):
    """Supported storage provider types."""
    NONE = "none"
    DROPBOX = "dropbox"
    S3 = "s3"


@dataclass(frozen=True)
class StorageFileInfo:
    """Metadata for a file stored in a remote provider."""
    # As the provider names it (Dropbox: a leading slash, lowercased by list_files while
    # upload_file and download_file keep the caller's casing; S3: no leading slash).
    # Providers accept their own form back, with or without the slash.
    remote_path: str
    # Provider-scoped digest: the Dropbox content hash, or BLAKE3/SHA256 on S3. Only
    # comparable within one provider. None when the provider call does not return one
    # (S3 list_files, or an S3 object without hash metadata).
    content_hash: str | None = None
    size_bytes: int = 0
    last_modified: str | None = None  # ISO 8601 timestamp


@dataclass(frozen=True)
class SyncResult:
    """Result of a sync (upload or download) operation."""
    uploaded: int = 0
    downloaded: int = 0
    skipped: int = 0
    failed: int = 0
    bytes_transferred: int = 0
    elapsed_seconds: float = 0.0
    errors: list[str] = field(default_factory=list)

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

        size_mb = self.bytes_transferred / BYTES_PER_MB
        parts.append(f"{size_mb:.2f} MB")
        parts.append(f"{self.elapsed_seconds:.1f}s")

        return ", ".join(parts)


@dataclass
class SyncTally:
    """Running counts of one directory sync; finish() turns them into a SyncResult."""
    started: float
    moved: int = 0  # files uploaded or downloaded
    skipped: int = 0
    failed: int = 0
    bytes_moved: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def done(self) -> int:
        return self.moved + self.skipped + self.failed

    def record(self, status: FileStatus, size: int = 0, error: str | None = None) -> None:
        """Count one file; an unknown status raises, as it must not read as a transfer."""
        if status == "skipped":
            self.skipped += 1
        elif status == "failed":
            self.failed += 1
            self.errors.append(error or "unknown error")
        elif status in ("uploaded", "downloaded"):
            self.moved += 1
            self.bytes_moved += size
        else:
            raise ValueError(f"Unknown sync status: {status!r}")

    def listing(self, list_files: Callable[[str], list[StorageFileInfo]], remote_directory: str) -> list[StorageFileInfo]:
        """The remote files to sync; a failed listing counts as one failure and lists nothing."""
        try:
            return list_files(remote_directory)
        except StorageError as exc:
            self.record("failed", error=str(exc))
            return []

    def finish(self, moved_field: MovedField, label: str) -> SyncResult:
        """Log and return the result."""
        result = SyncResult(
            skipped=self.skipped,
            failed=self.failed,
            bytes_transferred=self.bytes_moved,
            elapsed_seconds=time.time() - self.started,
            errors=self.errors,
            **{moved_field: self.moved},
        )
        logger.info(f"{label}: {result.summary()}")
        return result


def upload_candidates(local_directory: str) -> Iterator[str]:
    """Every non-hidden file; never walks into .reddit-stash/ or other hidden directories."""
    for root, dirs, fnames in os.walk(local_directory):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for fname in fnames:
            if not fname.startswith("."):
                yield os.path.join(root, fname)


def split_log_file(paths: Iterable[str]) -> tuple[list[str], str | None]:
    """Separate file_log.json from the content files, as it must be uploaded after them."""
    regular, log_path = [], None
    for path in paths:
        if os.path.basename(path) == LOG_FILE_NAME:
            log_path = path
        else:
            regular.append(path)
    return regular, log_path


@runtime_checkable
class StorageProviderProtocol(Protocol):
    """Protocol that all storage backends must implement.

    upload_file, download_file, list_files and get_file_info raise StorageError on any
    failure. The directory and log syncs count per-file failures in their SyncResult.
    """

    def connect(self) -> None:
        """Establish connection / refresh credentials."""
        ...

    def upload_file(self, local_path: str, remote_path: str) -> StorageFileInfo:
        """Upload a single file. Returns its metadata; raises StorageError on failure."""
        ...

    def download_file(self, remote_path: str, local_path: str) -> StorageFileInfo:
        """Download a single file. Returns its metadata; raises StorageError on failure."""
        ...

    def list_files(self, remote_directory: str) -> list[StorageFileInfo]:
        """List all files under a remote directory (recursive); raises StorageError on failure."""
        ...

    def get_file_info(self, remote_path: str) -> StorageFileInfo | None:
        """Get metadata for a single remote file, None if not found; raises StorageError otherwise."""
        ...

    def file_exists(self, remote_path: str) -> bool:
        """Check whether a remote file exists."""
        return self.get_file_info(remote_path) is not None

    def upload_directory(self, local_directory: str, remote_directory: str) -> SyncResult:
        """Upload a local directory, file_log.json last and only if all content succeeded.

        A failed remote listing raises StorageError on S3; Dropbox uploads without change detection.
        """
        ...

    def download_directory(self, remote_directory: str, local_directory: str) -> SyncResult:
        """Download a remote directory; a failed listing and keys resolving outside local_directory count as failed."""
        ...

    def download_log(self, remote_directory: str, local_directory: str) -> SyncResult:
        """Download only file_log.json; a missing log is not a failure, any other error counts as failed."""
        ...

    def get_provider_name(self) -> str:
        """Return the human-readable provider name."""
        ...
