"""
AWS S3 storage provider implementing StorageProviderProtocol.

Uses boto3 credential chain (env vars, IAM roles, OIDC).
BLAKE3 file hashes are stored as S3 user metadata for cross-provider comparison.
"""

import os
import signal
import time
from typing import Dict, List, Optional

from utils.storage.base import StorageFileInfo, SyncResult
from utils.storage.content_hash import compute_file_hash, hashes_match
from utils.storage.s3_lease import S3Lease, is_control_key

# Lazy import — boto3 may not be installed
_boto3 = None
_botocore = None
_TransferConfig = None

MULTIPART_THRESHOLD = 8 * 1024 * 1024  # 8 MB
BLAKE3_META_KEY = "blake3"

# Storage classes that have minimum storage duration charges
GLACIER_CLASSES = frozenset({
    "GLACIER_IR", "GLACIER", "DEEP_ARCHIVE",
})

VALID_STORAGE_CLASSES = frozenset({
    "STANDARD", "STANDARD_IA", "ONEZONE_IA", "INTELLIGENT_TIERING",
    "GLACIER_IR", "GLACIER", "DEEP_ARCHIVE",
})


def _ensure_boto3():
    global _boto3, _botocore, _TransferConfig
    if _boto3 is None:
        import boto3
        import botocore
        from boto3.s3.transfer import TransferConfig

        _boto3 = boto3
        _botocore = botocore
        _TransferConfig = TransferConfig


def _fmt_size(nbytes: int) -> str:
    """Format byte count as human-readable size."""
    for unit in ("B", "KB", "MB", "GB"):
        if abs(nbytes) < 1024:
            return f"{nbytes:.1f} {unit}"
        nbytes /= 1024
    return f"{nbytes:.1f} TB"


def _fmt_duration(seconds: float) -> str:
    """Format seconds as human-readable duration."""
    if seconds < 60:
        return f"{seconds:.0f}s"
    minutes = int(seconds // 60)
    secs = int(seconds % 60)
    return f"{minutes}m{secs:02d}s"


class S3StorageProvider:
    """AWS S3 implementation of StorageProviderProtocol."""

    def __init__(
        self,
        bucket: str,
        region: Optional[str] = None,
        storage_class: str = "STANDARD_IA",
        endpoint_url: Optional[str] = None,
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
        self._log_etag: Optional[str] = None  # None with a snapshot = log was absent

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
        _ensure_boto3()

        # Determine if SSL should be disabled for non-AWS endpoints
        use_ssl = True
        if self._endpoint_url and "amazonaws.com" not in self._endpoint_url:
            use_ssl = self._endpoint_url.startswith("https")

        session = _boto3.Session(region_name=self._region)
        retry_config = _botocore.config.Config(
            retries={"mode": "adaptive", "max_attempts": 5},
        )

        self._s3 = session.client(
            "s3",
            endpoint_url=self._endpoint_url,
            use_ssl=use_ssl,
            config=retry_config,
        )
        self._transfer_config = _TransferConfig(
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

        print(f" -- S3 connected: s3://{self._bucket} ({self._storage_class}) -- ")

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
        sc = "STANDARD" if os.path.basename(local_path) == "file_log.json" else self._storage_class

        extra_args: Dict = {
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

    def list_files(self, remote_directory: str) -> List[StorageFileInfo]:
        self._require_client()
        prefix = remote_directory.strip("/")
        if prefix:
            prefix += "/"

        result: List[StorageFileInfo] = []
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

    def get_file_info(self, remote_path: str) -> Optional[StorageFileInfo]:
        self._require_client()
        remote_key = remote_path.lstrip("/")

        try:
            resp = self._s3.head_object(Bucket=self._bucket, Key=remote_key)
            meta = resp.get("Metadata", {})
            return StorageFileInfo(
                remote_path=remote_key,
                content_hash=meta.get(BLAKE3_META_KEY),
                size_bytes=resp["ContentLength"],
                last_modified=resp["LastModified"].isoformat(),
            )
        except self._s3.exceptions.ClientError as exc:
            if exc.response["Error"]["Code"] == "404":
                return None
            raise

    def file_exists(self, remote_path: str) -> bool:
        return self.get_file_info(remote_path) is not None

    def upload_directory(self, local_directory: str, remote_directory: str,
                         check_type: str = "DIR") -> SyncResult:
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
            print("\nSIGTERM received — finishing current upload, will skip file_log.json.")

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
                             start: float, is_cancelled) -> SyncResult:
        files = []
        for root, dirs, fnames in os.walk(local_directory):
            dirs[:] = [d for d in dirs if not d.startswith(".")]  # never walk into .reddit-stash/ etc.
            files.extend((root, fname) for fname in fnames if not fname.startswith("."))

        # Separate file_log.json to upload last
        log_entry = None
        regular_files = []
        for root, fname in files:
            if fname == "file_log.json":
                log_entry = (root, fname)
            else:
                regular_files.append((root, fname))

        total_files = len(regular_files)

        # Nothing to upload — just sync file_log.json and return
        if total_files == 0:
            print("S3 upload: no content files to upload")
            uploaded = 0
            if log_entry:
                try:
                    self._upload_log(
                        os.path.join(log_entry[0], log_entry[1]),
                        f"{remote_directory.strip('/')}/file_log.json"
                        if remote_directory.strip("/") else "file_log.json",
                    )
                    uploaded = 1
                    print("  file_log.json synced")
                except Exception as exc:
                    elapsed = time.time() - start
                    return SyncResult(failed=1, elapsed_seconds=elapsed, errors=[str(exc)])
            elapsed = time.time() - start
            result = SyncResult(uploaded=uploaded, elapsed_seconds=elapsed)
            print(f"S3 upload complete: {result.summary()}")
            return result

        # Pre-compute total size for progress reporting
        total_bytes = sum(
            os.path.getsize(os.path.join(root, fname))
            for root, fname in regular_files
        )

        # Build a set of known remote keys — only needed when there are files to compare.
        # BLAKE3 hash is fetched lazily per-file only when a remote key exists.
        remote_keys = {info.remote_path for info in self.list_files(remote_directory)}

        print(f"S3 upload: {total_files} files ({_fmt_size(total_bytes)}) to process, "
              f"{len(remote_keys)} remote files")

        uploaded = 0
        skipped = 0
        failed = 0
        bytes_transferred = 0
        bytes_processed = 0
        errors: List[str] = []
        last_progress_time = time.time()

        def _upload_one(root: str, fname: str, show_progress: bool = True):
            nonlocal uploaded, skipped, failed, bytes_transferred, bytes_processed, last_progress_time
            file_path = os.path.join(root, fname)
            file_size = os.path.getsize(file_path)
            rel = os.path.relpath(file_path, local_directory).replace(os.sep, "/")
            prefix = remote_directory.strip("/")
            remote_key = f"{prefix}/{rel}" if prefix else rel

            local_hash = compute_file_hash(file_path)

            # Only HEAD the remote file if it exists — avoids N+1 queries
            if remote_key in remote_keys:
                remote_info = self.get_file_info(remote_key)
                if remote_info and remote_info.content_hash and hashes_match(remote_info.content_hash, local_hash):
                    skipped += 1
                    bytes_processed += file_size
                    return

            try:
                info = self.upload_file(file_path, remote_key)
                uploaded += 1
                bytes_transferred += info.size_bytes
                bytes_processed += file_size
            except Exception as exc:
                failed += 1
                bytes_processed += file_size
                errors.append(f"{file_path}: {exc}")

            # Print progress every 10 seconds or on every file if fewer than 20
            if show_progress:
                now = time.time()
                done = uploaded + skipped + failed
                if total_files <= 20 or (now - last_progress_time) >= 10 or done == total_files:
                    last_progress_time = now
                    pct = (done / total_files * 100) if total_files else 100
                    elapsed = now - start
                    eta = ""
                    if done > 0 and done < total_files:
                        eta_secs = (elapsed / done) * (total_files - done)
                        eta = f" | ETA: ~{_fmt_duration(eta_secs)}"
                    print(
                        f"  [{done}/{total_files}] {pct:.0f}% | "
                        f"{_fmt_size(bytes_transferred)} uploaded, "
                        f"{skipped} skipped, {failed} failed{eta}"
                    )

        for root, fname in regular_files:
            if is_cancelled():
                failed += len(regular_files) - (uploaded + skipped + failed)
                errors.append("Upload interrupted by SIGTERM")
                break
            _upload_one(root, fname)

        # Upload file_log.json ONLY if all regular files succeeded and not cancelled
        if log_entry:
            if failed > 0 or is_cancelled():
                print(f"SKIPPING file_log.json upload: {failed} file(s) failed or upload was cancelled.")
                print("This prevents marking items as processed when their backup files are missing.")
            elif not self.require_log_snapshot:
                _upload_one(*log_entry, show_progress=False)
            else:
                log_path = os.path.join(*log_entry)
                prefix = remote_directory.strip("/")
                rel = os.path.relpath(log_path, local_directory).replace(os.sep, "/")
                try:
                    info = self._upload_log(log_path, f"{prefix}/{rel}" if prefix else rel)
                    uploaded += 1
                    bytes_transferred += info.size_bytes
                except Exception as exc:
                    failed += 1
                    errors.append(f"{log_path}: {exc}")

        elapsed = time.time() - start
        result = SyncResult(
            uploaded=uploaded,
            skipped=skipped,
            failed=failed,
            bytes_transferred=bytes_transferred,
            elapsed_seconds=elapsed,
            errors=errors,
        )
        print(f"S3 upload complete: {result.summary()}")
        return result

    def download_directory(self, remote_directory: str, local_directory: str,
                           check_type: str = "DIR") -> SyncResult:
        self._require_client()
        start = time.time()

        if check_type == "LOG":
            return self._download_log_only(remote_directory, local_directory, start)

        remote_files = self.list_files(remote_directory)
        total_files = len(remote_files)

        # Nothing in remote storage — return immediately
        if total_files == 0:
            print("S3 download: no remote files to download")
            elapsed = time.time() - start
            result = SyncResult(elapsed_seconds=elapsed)
            print(f"S3 download complete: {result.summary()}")
            return result

        total_bytes = sum(f.size_bytes for f in remote_files)
        print(f"S3 download: {total_files} files ({_fmt_size(total_bytes)}) to process")

        downloaded = 0
        skipped = 0
        failed = 0
        bytes_transferred = 0
        errors: List[str] = []
        last_progress_time = time.time()

        prefix = remote_directory.strip("/")
        prefix_len = len(prefix) + 1 if prefix else 0

        for info in remote_files:
            rel_path = info.remote_path[prefix_len:]
            local_path = os.path.join(local_directory, rel_path)

            # Skip if local file already exists and matches remote hash.
            # HEAD is only called when the local file exists (common in Docker/local mode,
            # never in GHA where the directory starts empty).
            if os.path.exists(local_path):
                remote_info = self.get_file_info(info.remote_path)
                if remote_info and remote_info.content_hash:
                    local_hash = compute_file_hash(local_path)
                    if hashes_match(local_hash, remote_info.content_hash):
                        skipped += 1
                        continue

            try:
                result_info = self.download_file(info.remote_path, local_path)
                downloaded += 1
                bytes_transferred += result_info.size_bytes
            except Exception as exc:
                failed += 1
                errors.append(f"{info.remote_path}: {exc}")

            # Progress reporting
            now = time.time()
            done = downloaded + skipped + failed
            if total_files <= 20 or (now - last_progress_time) >= 10 or done == total_files:
                last_progress_time = now
                pct = (done / total_files * 100) if total_files else 100
                elapsed_so_far = now - start
                eta = ""
                if done > 0 and done < total_files:
                    eta_secs = (elapsed_so_far / done) * (total_files - done)
                    eta = f" | ETA: ~{_fmt_duration(eta_secs)}"
                print(
                    f"  [{done}/{total_files}] {pct:.0f}% | "
                    f"{_fmt_size(bytes_transferred)} downloaded, "
                    f"{skipped} skipped, {failed} failed{eta}"
                )

        elapsed = time.time() - start
        result = SyncResult(
            downloaded=downloaded,
            skipped=skipped,
            failed=failed,
            bytes_transferred=bytes_transferred,
            elapsed_seconds=elapsed,
            errors=errors,
        )
        print(f"S3 download complete: {result.summary()}")
        return result

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

    def _download_log_snapshot(self, remote_key: str, local_path: str, start: float) -> SyncResult:
        """Download file_log.json and remember the ETag of exactly this body."""
        try:
            resp = self._s3.get_object(Bucket=self._bucket, Key=remote_key)
        except self._s3.exceptions.ClientError as exc:
            if exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode") == 404:
                self._log_etag, self._log_snapshot_taken = None, True
                print("No existing log file in S3 — starting fresh.")
                return SyncResult(elapsed_seconds=time.time() - start)
            print(f"Failed to download log file from S3: {exc}")
            return SyncResult(failed=1, elapsed_seconds=time.time() - start, errors=[str(exc)])
        data = resp["Body"].read()
        os.makedirs(os.path.dirname(local_path) or ".", exist_ok=True)
        with open(local_path, "wb") as f:
            f.write(data)
        self._log_etag, self._log_snapshot_taken = resp["ETag"], True
        print(f"Log file downloaded to {local_path}.")
        return SyncResult(downloaded=1, bytes_transferred=len(data), elapsed_seconds=time.time() - start)

    def _upload_log(self, local_path: str, remote_key: str) -> StorageFileInfo:
        """Upload file_log.json; in coordinated runs only if it is unchanged since download."""
        if not self.require_log_snapshot:
            return self.upload_file(local_path, remote_key)
        if not self._log_snapshot_taken:
            raise RuntimeError("file_log.json was not downloaded in this run; refusing to overwrite it")
        condition = {"IfMatch": self._log_etag} if self._log_etag else {"IfNoneMatch": "*"}
        with open(local_path, "rb") as f:
            data = f.read()
        try:
            resp = self._s3.put_object(
                Bucket=self._bucket, Key=remote_key.lstrip("/"), Body=data,
                ServerSideEncryption="AES256", StorageClass="STANDARD",
                Metadata={BLAKE3_META_KEY: compute_file_hash(local_path)},
                **condition,
            )
        except self._s3.exceptions.ClientError as exc:
            if exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode") in (404, 409, 412):
                raise RuntimeError(
                    "file_log.json changed in S3 since this run downloaded it; "
                    "not overwriting another runner's log"
                ) from exc
            raise
        self._log_etag = resp["ETag"]
        return StorageFileInfo(remote_path=remote_key.lstrip("/"), size_bytes=len(data))

    def _download_log_only(self, remote_directory: str, local_directory: str,
                           start: float) -> SyncResult:
        """Download only file_log.json."""
        prefix = remote_directory.strip("/")
        remote_key = f"{prefix}/file_log.json" if prefix else "file_log.json"
        local_path = os.path.join(local_directory, "file_log.json")

        if self.require_log_snapshot:
            return self._download_log_snapshot(remote_key, local_path, start)

        try:
            info = self.download_file(remote_key, local_path)
            print(f"Log file downloaded to {local_path}.")
            return SyncResult(
                downloaded=1,
                bytes_transferred=info.size_bytes,
                elapsed_seconds=time.time() - start,
            )
        except self._s3.exceptions.ClientError as exc:
            if exc.response["Error"]["Code"] == "404":
                print("No existing log file in S3 — starting fresh.")
                return SyncResult(elapsed_seconds=time.time() - start)
            print(f"Failed to download log file from S3: {exc}")
            return SyncResult(
                failed=1,
                elapsed_seconds=time.time() - start,
                errors=[str(exc)],
            )
        except Exception as exc:
            print(f"Failed to download log file from S3: {exc}")
            return SyncResult(
                failed=1,
                elapsed_seconds=time.time() - start,
                errors=[str(exc)],
            )
