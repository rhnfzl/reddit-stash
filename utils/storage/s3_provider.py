"""
AWS S3 storage provider implementing StorageProviderProtocol.

Uses boto3 credential chain (env vars, IAM roles, OIDC).
BLAKE3 file hashes are stored as S3 user metadata for cross-provider comparison.
"""

import logging
import os
import signal
import time
from collections.abc import Callable

from utils.constants import BYTES_PER_MB, LOG_FILE_NAME
from utils.path_security import is_path_within
from utils.storage.base import (
    FileOutcome,
    StorageFileInfo,
    StorageProviderProtocol,
    SyncResult,
    SyncTally,
    raises_storage_error,
    split_log_file,
    upload_candidates,
)
from utils.storage.content_hash import compute_file_hash, hashes_match
from utils.storage.s3_lease import S3Lease, http_status, is_control_key
from utils.storage.sync_progress import SyncProgress, format_size

logger = logging.getLogger(__name__)

MULTIPART_THRESHOLD = 8 * BYTES_PER_MB
BLAKE3_META_KEY = "blake3"

# Storage classes that have minimum storage duration charges
GLACIER_CLASSES = frozenset({
    "GLACIER_IR", "GLACIER", "DEEP_ARCHIVE",
})

VALID_STORAGE_CLASSES = frozenset({
    "STANDARD", "STANDARD_IA", "ONEZONE_IA", "INTELLIGENT_TIERING",
    "GLACIER_IR", "GLACIER", "DEEP_ARCHIVE",
})


class S3StorageProvider(StorageProviderProtocol):
    """AWS S3 implementation of StorageProviderProtocol."""

    def __init__(
        self,
        bucket: str,
        region: str | None = None,
        storage_class: str = "STANDARD_IA",
        endpoint_url: str | None = None,
    ):
        self._bucket = bucket
        self._region = region
        self._storage_class = storage_class.upper()
        self._endpoint_url = endpoint_url
        self._s3 = None
        self._transfer_config = None
        # Coordinated runs (storage_utils --cycle) set require_log_snapshot so
        # file_log.json is only committed if nobody changed it since our download.
        self.require_log_snapshot = False
        self._log_snapshot_taken = False
        self._log_etag: str | None = None  # None with a snapshot = log was absent

        if self._storage_class not in VALID_STORAGE_CLASSES:
            raise ValueError(
                f"Invalid storage class '{self._storage_class}'. "
                f"Must be one of: {', '.join(sorted(VALID_STORAGE_CLASSES))}"
            )

    # ------------------------------------------------------------------
    # Protocol methods
    # ------------------------------------------------------------------

    def connect(self) -> None:
        """Create an S3 client using the boto3 credential chain."""
        # boto3 is optional (requirements-s3.txt), so Dropbox-only installs must not import it here.
        import boto3
        from boto3.s3.transfer import TransferConfig
        from botocore.config import Config

        # Determine if SSL should be disabled for non-AWS endpoints
        use_ssl = True
        if self._endpoint_url and "amazonaws.com" not in self._endpoint_url:
            use_ssl = self._endpoint_url.startswith("https")

        session = boto3.Session(region_name=self._region)
        retry_config = Config(
            retries={"mode": "adaptive", "max_attempts": 5},
        )

        self._s3 = session.client(
            "s3",
            endpoint_url=self._endpoint_url,
            use_ssl=use_ssl,
            config=retry_config,
        )
        self._transfer_config = TransferConfig(
            multipart_threshold=MULTIPART_THRESHOLD,
        )

        # Verify bucket access
        try:
            self._s3.head_bucket(Bucket=self._bucket)
        except Exception as exc:
            raise RuntimeError(
                f"Cannot access S3 bucket '{self._bucket}': {exc}\n"
                "Ensure the bucket exists and your credentials have access."
            ) from exc

        logger.info(f" -- S3 connected: s3://{self._bucket} ({self._storage_class}) -- ")

    @raises_storage_error
    def upload_file(self, local_path: str, remote_path: str) -> StorageFileInfo:
        self._require_client()
        remote_key = remote_path.lstrip("/")
        file_hash = compute_file_hash(local_path)
        file_size = os.path.getsize(local_path)

        # Check for overwrite protection (Glacier classes have 90-day minimum)
        if self._storage_class in GLACIER_CLASSES:
            existing = self.get_file_info(remote_key)
            if existing and hashes_match(existing.content_hash, file_hash):
                return existing  # Skip — identical content, avoid Glacier charges

        # Determine storage class: file_log.json always STANDARD
        sc = "STANDARD" if os.path.basename(local_path) == LOG_FILE_NAME else self._storage_class

        extra_args: dict = {
            "ServerSideEncryption": "AES256",
            "StorageClass": sc,
            "Metadata": {BLAKE3_META_KEY: file_hash},
        }

        self._s3.upload_file(
            local_path, self._bucket, remote_key,
            ExtraArgs=extra_args,
            Config=self._transfer_config,
        )

        return StorageFileInfo(
            remote_path=remote_key,
            content_hash=file_hash,
            size_bytes=file_size,
        )

    @raises_storage_error
    def download_file(self, remote_path: str, local_path: str) -> StorageFileInfo:
        self._require_client()
        remote_key = remote_path.lstrip("/")

        os.makedirs(os.path.dirname(local_path) or ".", exist_ok=True)
        self._s3.download_file(
            self._bucket, remote_key, local_path,
            Config=self._transfer_config,
        )

        file_hash = compute_file_hash(local_path)
        size = os.path.getsize(local_path)

        return StorageFileInfo(
            remote_path=remote_key,
            content_hash=file_hash,
            size_bytes=size,
        )

    @raises_storage_error
    def list_files(self, remote_directory: str) -> list[StorageFileInfo]:
        self._require_client()
        prefix = remote_directory.strip("/")
        if prefix:
            prefix += "/"

        result: list[StorageFileInfo] = []
        paginator = self._s3.get_paginator("list_objects_v2")

        for page in paginator.paginate(Bucket=self._bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                if is_control_key(obj["Key"]):
                    continue  # lease/heartbeat object, never synced
                result.append(StorageFileInfo(
                    remote_path=obj["Key"],
                    size_bytes=obj["Size"],
                    last_modified=obj["LastModified"].isoformat(),
                ))

        return result

    @raises_storage_error
    def get_file_info(self, remote_path: str) -> StorageFileInfo | None:
        self._require_client()
        remote_key = remote_path.lstrip("/")

        try:
            resp = self._s3.head_object(Bucket=self._bucket, Key=remote_key)
        except Exception as exc:
            if http_status(exc) == 404:
                return None
            raise
        return StorageFileInfo(
            remote_path=remote_key,
            content_hash=resp.get("Metadata", {}).get(BLAKE3_META_KEY),
            size_bytes=resp["ContentLength"],
            last_modified=resp["LastModified"].isoformat(),
        )

    def upload_directory(self, local_directory: str, remote_directory: str) -> SyncResult:
        self._require_client()
        start = time.time()

        # Block SIGTERM during upload to prevent partial state.
        # If cancelled mid-upload, we want to either finish cleanly or
        # skip file_log.json entirely — never upload the log without the data.
        sigterm_received = False
        original_handler = signal.getsignal(signal.SIGTERM)

        def _deferred_sigterm(signum, frame):
            nonlocal sigterm_received
            sigterm_received = True
            logger.warning("SIGTERM received — finishing current upload, will skip file_log.json.")

        signal.signal(signal.SIGTERM, _deferred_sigterm)

        try:
            return self._do_upload_directory(
                local_directory, remote_directory, start,
                lambda: sigterm_received,
            )
        finally:
            signal.signal(signal.SIGTERM, original_handler)
            if sigterm_received:
                # Re-raise SIGTERM so the process exits after cleanup
                os.kill(os.getpid(), signal.SIGTERM)

    def _do_upload_directory(self, local_directory: str, remote_directory: str,
                             start: float, is_cancelled: Callable[[], bool]) -> SyncResult:
        regular_files, log_path = split_log_file(upload_candidates(local_directory))
        tally = SyncTally(start)

        def remote_key(path: str) -> str:
            rel = os.path.relpath(path, local_directory).replace(os.sep, "/")
            prefix = remote_directory.strip("/")
            return f"{prefix}/{rel}" if prefix else rel

        # Nothing to upload — just sync file_log.json and return
        if not regular_files:
            logger.info("S3 upload: no content files to upload")
            if log_path:
                # Always the root key here, wherever the log was found: the S3 layout is frozen.
                root_log = os.path.join(local_directory, LOG_FILE_NAME)
                # No listing was fetched, so nothing is compared and the log is always written.
                self._sync_log(tally, log_path, remote_key(root_log), set(), is_cancelled)
            return tally.finish("uploaded", "S3 upload complete")

        total_files = len(regular_files)
        total_bytes = sum(os.path.getsize(path) for path in regular_files)

        # Build a set of known remote keys — only needed when there are files to compare.
        # BLAKE3 hash is fetched lazily per-file only when a remote key exists.
        remote_keys = {info.remote_path for info in self.list_files(remote_directory)}

        logger.info(f"S3 upload: {total_files} files ({format_size(total_bytes)}) to process, "
                    f"{len(remote_keys)} remote files")

        progress = SyncProgress("uploaded", total_files, start)
        for path in regular_files:
            if is_cancelled():
                tally.failed += total_files - tally.done
                tally.errors.append("Upload interrupted by SIGTERM")
                break
            tally.record(*self._upload_changed(path, remote_key(path), remote_keys))
            progress.report(tally)

        if log_path:
            self._sync_log(tally, log_path, remote_key(log_path), remote_keys, is_cancelled)

        return tally.finish("uploaded", "S3 upload complete")

    def _sync_log(self, tally: SyncTally, log_path: str, key: str, remote_keys: set[str],
                  is_cancelled: Callable[[], bool]) -> None:
        """Upload file_log.json ONLY if all regular files succeeded and not cancelled."""
        if tally.failed or is_cancelled():
            logger.error(f"SKIPPING file_log.json upload: {tally.failed} file(s) failed or upload was cancelled.")
            logger.error("This prevents marking items as processed when their backup files are missing.")
        elif self.require_log_snapshot:
            tally.record(*self._commit_log(log_path, key))
        else:
            tally.record(*self._upload_changed(log_path, key, remote_keys))

    def _upload_changed(self, file_path: str, remote_key: str, remote_keys: set[str]) -> FileOutcome:
        """Upload one file unless the remote copy has the same hash."""
        try:
            # Only HEAD the remote file if it exists — avoids N+1 queries
            if remote_key in remote_keys:
                remote_info = self.get_file_info(remote_key)
                if remote_info and hashes_match(remote_info.content_hash, compute_file_hash(file_path)):
                    return "skipped", 0, None
            return "uploaded", self.upload_file(file_path, remote_key).size_bytes, None
        except Exception as exc:
            return "failed", 0, f"{file_path}: {exc}"

    def _commit_log(self, log_path: str, remote_key: str) -> FileOutcome:
        try:
            return "uploaded", self._upload_log(log_path, remote_key).size_bytes, None
        except Exception as exc:
            return "failed", 0, f"{log_path}: {exc}"

    def download_directory(self, remote_directory: str, local_directory: str) -> SyncResult:
        self._require_client()
        tally = SyncTally(time.time())

        remote_files = tally.listing(self.list_files, remote_directory)

        # Nothing in remote storage, or the listing failed — return immediately
        if not remote_files:
            if not tally.failed:
                logger.info("S3 download: nothing to download")
            return tally.finish("downloaded", "S3 download complete")

        total_bytes = sum(f.size_bytes for f in remote_files)
        logger.info(f"S3 download: {len(remote_files)} files ({format_size(total_bytes)}) to process")

        prefix = remote_directory.strip("/")
        prefix_len = len(prefix) + 1 if prefix else 0

        progress = SyncProgress("downloaded", len(remote_files), tally.started)
        for info in remote_files:
            local_path = os.path.join(local_directory, info.remote_path[prefix_len:])
            tally.record(*self._download_changed(info.remote_path, local_path, local_directory))
            progress.report(tally)

        return tally.finish("downloaded", "S3 download complete")

    def _download_changed(self, remote_key: str, local_path: str, local_directory: str) -> FileOutcome:
        """Download one file unless the local copy has the same hash."""
        if not is_path_within(local_directory, local_path):
            return "failed", 0, f"{remote_key}: resolves outside {local_directory}"

        try:
            # HEAD is only called when the local file exists (common in Docker/local mode,
            # never in GHA where the directory starts empty).
            if os.path.exists(local_path):
                remote_info = self.get_file_info(remote_key)
                if remote_info and remote_info.content_hash:
                    if hashes_match(compute_file_hash(local_path), remote_info.content_hash):
                        return "skipped", 0, None

            return "downloaded", self.download_file(remote_key, local_path).size_bytes, None
        except Exception as exc:
            return "failed", 0, f"{remote_key}: {exc}"

    def download_log(self, remote_directory: str, local_directory: str) -> SyncResult:
        self._require_client()
        start = time.time()
        prefix = remote_directory.strip("/")
        remote_key = f"{prefix}/{LOG_FILE_NAME}" if prefix else LOG_FILE_NAME
        local_path = os.path.join(local_directory, LOG_FILE_NAME)

        try:
            if self.require_log_snapshot:
                size = self._download_log_snapshot(remote_key, local_path)
            else:
                size = self.download_file(remote_key, local_path).size_bytes
        except Exception as exc:
            if http_status(exc.__cause__ or exc) == 404:
                self._log_etag, self._log_snapshot_taken = None, True
                logger.info("No existing log file in S3 — starting fresh.")
                return SyncResult(elapsed_seconds=time.time() - start)
            logger.error(f"Failed to download log file from S3: {exc}")
            return SyncResult(failed=1, elapsed_seconds=time.time() - start, errors=[str(exc)])

        logger.info(f"Log file downloaded to {local_path}.")
        return SyncResult(downloaded=1, bytes_transferred=size, elapsed_seconds=time.time() - start)

    def get_provider_name(self) -> str:
        return "AWS S3"

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _require_client(self):
        if self._s3 is None:
            raise RuntimeError("Call connect() before using the S3 provider.")

    def lease(self, prefix: str, runner: str) -> S3Lease:
        """Run lease for the dataset stored under prefix."""
        self._require_client()
        return S3Lease(self._s3, self._bucket, prefix, runner)

    def _download_log_snapshot(self, remote_key: str, local_path: str) -> int:
        """Download file_log.json, remember the ETag of exactly this body, return its size."""
        resp = self._s3.get_object(Bucket=self._bucket, Key=remote_key)
        data = resp["Body"].read()
        os.makedirs(os.path.dirname(local_path) or ".", exist_ok=True)
        with open(local_path, "wb") as f:
            f.write(data)
        self._log_etag, self._log_snapshot_taken = resp["ETag"], True
        return len(data)

    def _upload_log(self, local_path: str, remote_key: str) -> StorageFileInfo:
        """Coordinated runs: upload file_log.json only if it is unchanged since download."""
        if not self._log_snapshot_taken:
            raise RuntimeError("file_log.json was not downloaded in this run; refusing to overwrite it")
        condition = {"IfMatch": self._log_etag} if self._log_etag else {"IfNoneMatch": "*"}
        with open(local_path, "rb") as f:
            data = f.read()
        key = remote_key.lstrip("/")
        try:
            resp = self._s3.put_object(
                Bucket=self._bucket, Key=key, Body=data,
                ServerSideEncryption="AES256", StorageClass="STANDARD",
                Metadata={BLAKE3_META_KEY: compute_file_hash(local_path)},
                **condition,
            )
            self._log_etag = resp["ETag"]
        except Exception as exc:
            # botocore retries: a 412 or timeout can follow our own write landing.
            try:
                current = self._s3.get_object(Bucket=self._bucket, Key=key)
                landed = current["Body"].read() == data
            except Exception:
                landed = False
            if landed:
                self._log_etag = current["ETag"]
            elif http_status(exc) in (404, 409, 412):
                raise RuntimeError(
                    "file_log.json changed in S3 since this run downloaded it; "
                    "not overwriting another runner's log"
                ) from exc
            else:
                raise
        return StorageFileInfo(remote_path=key, size_bytes=len(data))
