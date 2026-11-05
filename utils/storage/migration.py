"""
Bidirectional migration tool between storage providers.

Downloads and uploads one file at a time through a temporary directory.
Supports dry-run mode (default) and explicit execution.
"""

import logging
import os
import tempfile
import time
from dataclasses import dataclass

from utils.constants import BYTES_PER_MB
from utils.storage.base import StorageFileInfo, StorageProviderProtocol, SyncResult

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MigrationPlan:
    """Describes what a migration would do, without executing."""
    source_provider: str
    target_provider: str
    file_count: int
    total_bytes: int
    files: list[StorageFileInfo]

    def summary(self) -> str:
        size_mb = self.total_bytes / BYTES_PER_MB
        return (
            f"Migration plan: {self.source_provider} -> {self.target_provider}\n"
            f"  Files: {self.file_count}\n"
            f"  Total size: {size_mb:.2f} MB"
        )


class StorageMigration:
    """Migrate files between any two storage providers."""

    def __init__(
        self,
        source: StorageProviderProtocol,
        target: StorageProviderProtocol,
        source_directory: str,
        target_directory: str,
    ):
        self._source = source
        self._target = target
        self._source_dir = source_directory
        self._target_dir = target_directory

    def dry_run(self) -> MigrationPlan:
        """List what would be transferred without making changes."""
        files = self._source.list_files(self._source_dir)
        total_bytes = sum(f.size_bytes for f in files)

        plan = MigrationPlan(
            source_provider=self._source.get_provider_name(),
            target_provider=self._target.get_provider_name(),
            file_count=len(files),
            total_bytes=total_bytes,
            files=files,
        )

        logger.info(plan.summary())
        return plan

    def execute(self) -> SyncResult:
        """Transfer each source file through a short-lived local directory."""
        start = time.time()
        source_files = self._source.list_files(self._source_dir)
        downloaded = 0
        failed_downloads = 0
        download_errors: list[str] = []
        uploaded = 0
        failed_uploads = 0
        upload_errors: list[str] = []
        bytes_transferred = 0

        src_prefix = self._source_dir.strip("/")
        src_prefix_len = len(src_prefix) + 1 if src_prefix else 0
        tgt_prefix = self._target_dir.strip("/")

        logger.info(
            f"Migrating from {self._source.get_provider_name()} to "
            f"{self._target.get_provider_name()}..."
        )
        for source_info in source_files:
            rel_path = source_info.remote_path[src_prefix_len:].lstrip("/")
            if not rel_path:
                rel_path = os.path.basename(source_info.remote_path)
            remote_key = f"{tgt_prefix}/{rel_path}" if tgt_prefix else rel_path

            try:
                with tempfile.TemporaryDirectory(prefix="reddit_stash_migrate_") as tmp_dir:
                    local_path = os.path.join(tmp_dir, os.path.basename(rel_path))
                    self._source.download_file(source_info.remote_path, local_path)
                    downloaded += 1

                    try:
                        uploaded_info = self._target.upload_file(local_path, remote_key)
                        uploaded += 1
                        bytes_transferred += uploaded_info.size_bytes
                    except Exception as exc:
                        failed_uploads += 1
                        upload_errors.append(f"upload {rel_path}: {exc}")
            except Exception as exc:
                failed_downloads += 1
                download_errors.append(f"download {source_info.remote_path}: {exc}")

        logger.info(f"Downloaded {downloaded} files ({failed_downloads} failed)")
        logger.info(f"Uploaded {uploaded} files ({failed_uploads} failed)")

        elapsed = time.time() - start
        all_errors = download_errors + upload_errors

        result = SyncResult(
            downloaded=downloaded,
            uploaded=uploaded,
            failed=failed_downloads + failed_uploads,
            bytes_transferred=bytes_transferred,
            elapsed_seconds=elapsed,
            errors=all_errors,
        )
        logger.info(f"Migration complete: {result.summary()}")
        return result
