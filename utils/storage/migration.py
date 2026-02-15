"""
Bidirectional migration tool between storage providers.

Downloads everything from source to a temp directory, then uploads
to target.  Supports dry-run mode (default) and explicit execution.
"""

import os
import shutil
import tempfile
import time
from dataclasses import dataclass
from typing import List

from utils.storage.base import StorageFileInfo, StorageProviderProtocol, SyncResult


@dataclass(frozen=True)
class MigrationPlan:
    """Describes what a migration would do, without executing."""
    source_provider: str
    target_provider: str
    file_count: int
    total_bytes: int
    files: List[StorageFileInfo]

    def summary(self) -> str:
        size_mb = self.total_bytes / (1024 * 1024)
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

        print(plan.summary())
        return plan

    def execute(self) -> SyncResult:
        """Download from source → upload to target via temp directory."""
        start = time.time()
        tmp_dir = tempfile.mkdtemp(prefix="reddit_stash_migrate_")

        try:
            # Phase 1: download everything from source
            print(f"Downloading from {self._source.get_provider_name()}...")
            source_files = self._source.list_files(self._source_dir)

            downloaded = 0
            failed_downloads = 0
            download_errors: List[str] = []

            # Determine prefix to strip from remote paths
            src_prefix = self._source_dir.strip("/")
            src_prefix_len = len(src_prefix) + 1 if src_prefix else 0

            for info in source_files:
                rel_path = info.remote_path[src_prefix_len:].lstrip("/")
                if not rel_path:
                    rel_path = os.path.basename(info.remote_path)
                local_path = os.path.join(tmp_dir, rel_path)

                try:
                    self._source.download_file(info.remote_path, local_path)
                    downloaded += 1
                except Exception as exc:
                    failed_downloads += 1
                    download_errors.append(f"download {info.remote_path}: {exc}")

            print(f"Downloaded {downloaded} files ({failed_downloads} failed)")

            # Phase 2: upload everything to target
            print(f"Uploading to {self._target.get_provider_name()}...")
            uploaded = 0
            failed_uploads = 0
            upload_errors: List[str] = []
            bytes_transferred = 0

            tgt_prefix = self._target_dir.strip("/")

            for root, _dirs, fnames in os.walk(tmp_dir):
                for fname in fnames:
                    file_path = os.path.join(root, fname)
                    rel = os.path.relpath(file_path, tmp_dir).replace(os.sep, "/")
                    remote_key = f"{tgt_prefix}/{rel}" if tgt_prefix else rel

                    try:
                        info = self._target.upload_file(file_path, remote_key)
                        uploaded += 1
                        bytes_transferred += info.size_bytes
                    except Exception as exc:
                        failed_uploads += 1
                        upload_errors.append(f"upload {rel}: {exc}")

            print(f"Uploaded {uploaded} files ({failed_uploads} failed)")

        finally:
            # Always clean up temp directory
            shutil.rmtree(tmp_dir, ignore_errors=True)

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
        print(f"Migration complete: {result.summary()}")
        return result
