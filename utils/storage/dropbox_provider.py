"""
Dropbox storage provider implementing StorageProviderProtocol.

Wraps the logic from dropbox_utils.py into a class-based provider
that can be used interchangeably with other storage backends.
"""

import hashlib
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Optional

from utils.storage.base import StorageFileInfo, SyncResult
from utils.storage.content_hash import compute_file_hash

# Lazy import — dropbox may not be installed
_dropbox = None
_ApiError = None
_FileMetadata = None

SINGLE_UPLOAD_LIMIT = 150 * 1024 * 1024  # 150 MB
UPLOAD_CHUNK_SIZE = 4 * 1024 * 1024  # 4 MB


def _ensure_dropbox():
    """Lazy-import the dropbox SDK."""
    global _dropbox, _ApiError, _FileMetadata
    if _dropbox is None:
        import dropbox
        from dropbox.exceptions import ApiError
        from dropbox.files import FileMetadata

        _dropbox = dropbox
        _ApiError = ApiError
        _FileMetadata = FileMetadata


class _DropboxContentHasher:
    """Dropbox content hash algorithm (4 MB blocks, SHA256)."""

    BLOCK_SIZE = 4 * 1024 * 1024

    def __init__(self):
        self._overall = hashlib.sha256()
        self._block = hashlib.sha256()
        self._block_pos = 0

    def update(self, data: bytes):
        pos = 0
        while pos < len(data):
            if self._block_pos == self.BLOCK_SIZE:
                self._overall.update(self._block.digest())
                self._block = hashlib.sha256()
                self._block_pos = 0
            space = self.BLOCK_SIZE - self._block_pos
            part = data[pos:pos + space]
            self._block.update(part)
            self._block_pos += len(part)
            pos += len(part)

    def hexdigest(self) -> str:
        if self._block_pos > 0:
            self._overall.update(self._block.digest())
        return self._overall.hexdigest()


def _dropbox_content_hash(file_path: str) -> str:
    """Compute the Dropbox-specific content hash for a local file."""
    hasher = _DropboxContentHasher()
    with open(file_path, "rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            hasher.update(chunk)
    return hasher.hexdigest()


def _sanitize_filename(name: str) -> str:
    """Make a filename safe for Dropbox."""
    sanitized = re.sub(r'[<>:"/\\|?*\x00-\x1F]', '_', name).strip()
    reserved = {
        "CON", "PRN", "AUX", "NUL",
        "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
        "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9",
    }
    if sanitized.upper() in reserved:
        sanitized = "_" + sanitized
    return sanitized


class DropboxStorageProvider:
    """Dropbox implementation of StorageProviderProtocol."""

    def __init__(self, dropbox_directory: str = "/reddit"):
        self._dropbox_directory = dropbox_directory
        self._dbx = None
        self._max_workers = 3

    # ------------------------------------------------------------------
    # Protocol methods
    # ------------------------------------------------------------------

    def connect(self) -> None:
        """Refresh the OAuth2 token and create a Dropbox client."""
        _ensure_dropbox()
        import requests as _requests

        refresh_token = os.getenv("DROPBOX_REFRESH_TOKEN")
        client_id = os.getenv("DROPBOX_APP_KEY")
        client_secret = os.getenv("DROPBOX_APP_SECRET")

        if not all([refresh_token, client_id, client_secret]):
            raise RuntimeError(
                "Missing Dropbox credentials. Set DROPBOX_REFRESH_TOKEN, "
                "DROPBOX_APP_KEY, and DROPBOX_APP_SECRET environment variables."
            )

        resp = _requests.post(
            "https://api.dropboxapi.com/oauth2/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": client_id,
                "client_secret": client_secret,
            },
        )

        if resp.status_code != 200:
            raise RuntimeError(f"Failed to refresh Dropbox token: {resp.text}")

        token = resp.json().get("access_token")
        os.environ["DROPBOX_TOKEN"] = token
        self._dbx = _dropbox.Dropbox(token)
        print(" -- Dropbox Access Token Refreshed -- ")

    def upload_file(self, local_path: str, remote_path: str) -> StorageFileInfo:
        self._require_client()
        size = self._raw_upload(local_path, remote_path)
        return StorageFileInfo(
            remote_path=remote_path,
            content_hash=compute_file_hash(local_path),
            size_bytes=size,
        )

    def download_file(self, remote_path: str, local_path: str) -> StorageFileInfo:
        self._require_client()
        os.makedirs(os.path.dirname(local_path), exist_ok=True)
        metadata, res = self._dbx.files_download(remote_path)
        with open(local_path, "wb") as f:
            f.write(res.content)
        return StorageFileInfo(
            remote_path=remote_path,
            content_hash=compute_file_hash(local_path),
            size_bytes=metadata.size,
        )

    def list_files(self, remote_directory: str) -> List[StorageFileInfo]:
        self._require_client()
        result_list: List[StorageFileInfo] = []
        try:
            result = self._dbx.files_list_folder(remote_directory, recursive=True)
            while True:
                for entry in result.entries:
                    if isinstance(entry, _FileMetadata):
                        result_list.append(StorageFileInfo(
                            remote_path=entry.path_lower,
                            content_hash=entry.content_hash,  # Dropbox hash, not BLAKE3
                            size_bytes=entry.size,
                        ))
                if not result.has_more:
                    break
                result = self._dbx.files_list_folder_continue(result.cursor)
        except _ApiError as err:
            print(f"Failed to list Dropbox folder {remote_directory}: {err}")
        return result_list

    def get_file_info(self, remote_path: str) -> Optional[StorageFileInfo]:
        self._require_client()
        try:
            meta = self._dbx.files_get_metadata(remote_path)
            if isinstance(meta, _FileMetadata):
                return StorageFileInfo(
                    remote_path=meta.path_lower,
                    content_hash=meta.content_hash,
                    size_bytes=meta.size,
                )
        except _ApiError:
            pass
        return None

    def file_exists(self, remote_path: str) -> bool:
        return self.get_file_info(remote_path) is not None

    def upload_directory(self, local_directory: str, remote_directory: str,
                         check_type: str = "DIR") -> SyncResult:
        self._require_client()
        start = time.time()

        # Build remote file hash map (Dropbox hashes)
        dbx_hashes = {}
        for info in self.list_files(remote_directory):
            dbx_hashes[info.remote_path] = info.content_hash

        files_to_upload = [
            (root, fname)
            for root, _dirs, files in os.walk(local_directory)
            for fname in files
            if not fname.startswith(".")
        ]

        uploaded = 0
        skipped = 0
        failed = 0
        bytes_transferred = 0
        errors: List[str] = []
        lock = threading.Lock()

        def _process(root_and_name):
            nonlocal uploaded, skipped, failed, bytes_transferred
            root, fname = root_and_name
            sanitized = _sanitize_filename(fname)
            file_path = os.path.join(root, fname)
            rel = os.path.relpath(file_path, local_directory).replace(os.sep, "/")
            dbx_path = f"{remote_directory}/{rel}".replace(fname, sanitized)

            # Skip unchanged files
            if dbx_path.lower() in dbx_hashes:
                local_hash = _dropbox_content_hash(file_path)
                if dbx_hashes[dbx_path.lower()] == local_hash:
                    with lock:
                        skipped += 1
                    return

            try:
                size = self._raw_upload(file_path, dbx_path)
                with lock:
                    uploaded += 1
                    bytes_transferred += size
            except Exception as exc:
                with lock:
                    failed += 1
                    errors.append(f"{file_path}: {exc}")

        with ThreadPoolExecutor(max_workers=self._max_workers) as pool:
            futures = {pool.submit(_process, f): f for f in files_to_upload}
            for future in as_completed(futures):
                future.result()  # propagate exceptions in _process

        elapsed = time.time() - start
        result = SyncResult(
            uploaded=uploaded,
            skipped=skipped,
            failed=failed,
            bytes_transferred=bytes_transferred,
            elapsed_seconds=elapsed,
            errors=errors,
        )
        print(f"Dropbox upload: {result.summary()}")
        return result

    def download_directory(self, remote_directory: str, local_directory: str,
                           check_type: str = "DIR") -> SyncResult:
        self._require_client()
        start = time.time()

        if check_type == "LOG":
            return self._download_log_only(remote_directory, local_directory, start)

        remote_files = self.list_files(remote_directory)

        downloaded = 0
        skipped = 0
        failed = 0
        bytes_transferred = 0
        errors: List[str] = []

        for info in remote_files:
            local_path = os.path.join(
                local_directory,
                info.remote_path[len(remote_directory):].lstrip("/"),
            )

            # Skip unchanged local files (compare Dropbox hashes)
            if os.path.exists(local_path):
                local_hash = _dropbox_content_hash(local_path)
                if local_hash == info.content_hash:
                    skipped += 1
                    continue

            try:
                os.makedirs(os.path.dirname(local_path), exist_ok=True)
                meta, res = self._dbx.files_download(info.remote_path)
                with open(local_path, "wb") as f:
                    f.write(res.content)
                downloaded += 1
                bytes_transferred += meta.size
            except Exception as exc:
                failed += 1
                errors.append(f"{info.remote_path}: {exc}")

        elapsed = time.time() - start
        result = SyncResult(
            downloaded=downloaded,
            skipped=skipped,
            failed=failed,
            bytes_transferred=bytes_transferred,
            elapsed_seconds=elapsed,
            errors=errors,
        )
        print(f"Dropbox download: {result.summary()}")
        return result

    def get_provider_name(self) -> str:
        return "Dropbox"

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _require_client(self):
        if self._dbx is None:
            raise RuntimeError("Call connect() before using the Dropbox provider.")

    def _raw_upload(self, local_path: str, remote_path: str) -> int:
        """Upload a single file, using chunked upload for large files."""
        file_size = os.path.getsize(local_path)

        if file_size <= SINGLE_UPLOAD_LIMIT:
            with open(local_path, "rb") as f:
                self._dbx.files_upload(
                    f.read(), remote_path,
                    mode=_dropbox.files.WriteMode.overwrite,
                )
        else:
            with open(local_path, "rb") as f:
                chunk = f.read(UPLOAD_CHUNK_SIZE)
                session = self._dbx.files_upload_session_start(chunk)
                cursor = _dropbox.files.UploadSessionCursor(
                    session_id=session.session_id, offset=len(chunk),
                )
                commit = _dropbox.files.CommitInfo(
                    path=remote_path,
                    mode=_dropbox.files.WriteMode.overwrite,
                )
                while True:
                    chunk = f.read(UPLOAD_CHUNK_SIZE)
                    if f.tell() >= file_size:
                        self._dbx.files_upload_session_finish(chunk, cursor, commit)
                        break
                    self._dbx.files_upload_session_append_v2(chunk, cursor)
                    cursor.offset += len(chunk)

        return file_size

    def _download_log_only(self, remote_directory: str, local_directory: str,
                           start: float) -> SyncResult:
        """Download only file_log.json."""
        log_remote = f"{remote_directory}/file_log.json"
        log_local = os.path.join(local_directory, "file_log.json")

        try:
            os.makedirs(os.path.dirname(log_local), exist_ok=True)
            meta, res = self._dbx.files_download(log_remote)
            with open(log_local, "wb") as f:
                f.write(res.content)
            print(f"Log file downloaded to {log_local}.")
            return SyncResult(
                downloaded=1,
                bytes_transferred=meta.size,
                elapsed_seconds=time.time() - start,
            )
        except _ApiError as exc:
            if exc.error.is_path() and exc.error.get_path().is_not_found():
                print("No existing log file in Dropbox — starting fresh.")
                return SyncResult(elapsed_seconds=time.time() - start)
            print(f"Failed to download log file: {exc}")
            return SyncResult(
                failed=1,
                elapsed_seconds=time.time() - start,
                errors=[str(exc)],
            )
        except Exception as exc:
            print(f"Failed to download log file: {exc}")
            return SyncResult(
                failed=1,
                elapsed_seconds=time.time() - start,
                errors=[str(exc)],
            )
