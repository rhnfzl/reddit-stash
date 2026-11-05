"""
Dropbox storage provider implementing StorageProviderProtocol.

Syncs the local save directory with a Dropbox folder, using Dropbox content
hashes to skip unchanged files.
"""

import hashlib
import logging
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import dropbox
import requests
from dropbox.exceptions import ApiError
from dropbox.files import FileMetadata

from utils.constants import BYTES_PER_MB, DEFAULT_TIMEOUT_SECONDS, LOG_FILE_NAME
from utils.path_security import is_path_within
from utils.storage.base import (
    FileOutcome,
    StorageError,
    StorageFileInfo,
    StorageProviderProtocol,
    SyncResult,
    SyncTally,
    raises_storage_error,
    split_log_file,
    upload_candidates,
)
from utils.storage.content_hash import CHUNK_SIZE

logger = logging.getLogger(__name__)

SINGLE_UPLOAD_LIMIT = 150 * BYTES_PER_MB
UPLOAD_CHUNK_SIZE = 4 * BYTES_PER_MB


class _DropboxContentHasher:
    """Dropbox content hash algorithm (4 MB blocks, SHA256)."""

    BLOCK_SIZE = 4 * BYTES_PER_MB

    def __init__(self):
        self._overall = hashlib.sha256()
        self._block = hashlib.sha256()
        self._block_pos = 0

    def update(self, data: bytes) -> None:
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
            chunk = f.read(CHUNK_SIZE)
            if not chunk:
                break
            hasher.update(chunk)
    return hasher.hexdigest()


def _api_path(remote_path: str) -> str:
    """Dropbox rejects paths without a leading slash; S3-style keys arrive without one."""
    return "/" + remote_path.lstrip("/")


def _is_not_found(err: ApiError) -> bool:
    return err.error.is_path() and err.error.get_path().is_not_found()


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


class DropboxStorageProvider(StorageProviderProtocol):
    """Dropbox implementation of StorageProviderProtocol."""

    def __init__(self):
        self._dbx = None
        self._max_workers = 3

    # ------------------------------------------------------------------
    # Protocol methods
    # ------------------------------------------------------------------

    def connect(self) -> None:
        """Refresh the OAuth2 token and create a Dropbox client."""
        refresh_token = os.getenv("DROPBOX_REFRESH_TOKEN")
        client_id = os.getenv("DROPBOX_APP_KEY")
        client_secret = os.getenv("DROPBOX_APP_SECRET")

        if not all([refresh_token, client_id, client_secret]):
            raise RuntimeError(
                "Missing Dropbox credentials. Set DROPBOX_REFRESH_TOKEN, "
                "DROPBOX_APP_KEY, and DROPBOX_APP_SECRET environment variables."
            )

        resp = requests.post(
            "https://api.dropboxapi.com/oauth2/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": client_id,
                "client_secret": client_secret,
            },
            timeout=DEFAULT_TIMEOUT_SECONDS,
        )

        if resp.status_code != 200:
            raise RuntimeError(f"Failed to refresh Dropbox token: {resp.text}")

        token = resp.json().get("access_token")
        self._dbx = dropbox.Dropbox(token)
        logger.info(" -- Dropbox Access Token Refreshed -- ")

    @raises_storage_error
    def upload_file(self, local_path: str, remote_path: str) -> StorageFileInfo:
        self._require_client()
        remote_path = _api_path(remote_path)
        size = self._raw_upload(local_path, remote_path)
        return StorageFileInfo(
            remote_path=remote_path,
            content_hash=_dropbox_content_hash(local_path),
            size_bytes=size,
        )

    @raises_storage_error
    def download_file(self, remote_path: str, local_path: str) -> StorageFileInfo:
        self._require_client()
        remote_path = _api_path(remote_path)
        size = self._download_to(remote_path, local_path)
        return StorageFileInfo(
            remote_path=remote_path,
            content_hash=_dropbox_content_hash(local_path),
            size_bytes=size,
        )

    @raises_storage_error
    def list_files(self, remote_directory: str) -> list[StorageFileInfo]:
        self._require_client()
        result_list: list[StorageFileInfo] = []
        try:
            result = self._dbx.files_list_folder(remote_directory, recursive=True)
            while True:
                for entry in result.entries:
                    if isinstance(entry, FileMetadata):
                        result_list.append(StorageFileInfo(
                            remote_path=entry.path_lower,
                            content_hash=entry.content_hash,  # Dropbox hash, not BLAKE3
                            size_bytes=entry.size,
                        ))
                if not result.has_more:
                    break
                result = self._dbx.files_list_folder_continue(result.cursor)
        except ApiError as err:
            # A missing folder is a legitimate empty remote (first run); any other
            # error would hand callers a partial listing that looks complete.
            if _is_not_found(err):
                return []
            raise StorageError(f"Failed to list Dropbox folder {remote_directory}: {err}") from err
        return result_list

    @raises_storage_error
    def get_file_info(self, remote_path: str) -> StorageFileInfo | None:
        self._require_client()
        try:
            meta = self._dbx.files_get_metadata(_api_path(remote_path))
        except ApiError as err:
            if _is_not_found(err):
                return None
            raise
        if isinstance(meta, FileMetadata):
            return StorageFileInfo(
                remote_path=meta.path_lower,
                content_hash=meta.content_hash,
                size_bytes=meta.size,
            )
        return None

    def upload_directory(self, local_directory: str, remote_directory: str) -> SyncResult:
        self._require_client()
        tally = SyncTally(time.time())
        dbx_hashes = self._remote_hashes(remote_directory)
        regular_files, log_path = split_log_file(upload_candidates(local_directory))

        def _upload_if_changed(file_path: str) -> FileOutcome:
            fname = os.path.basename(file_path)
            rel = os.path.relpath(file_path, local_directory).replace(os.sep, "/")
            dbx_path = f"{remote_directory}/{rel}".replace(fname, _sanitize_filename(fname))

            try:
                # Skip unchanged files
                if dbx_path.lower() in dbx_hashes:
                    local_hash = _dropbox_content_hash(file_path)
                    if dbx_hashes[dbx_path.lower()] == local_hash:
                        return "skipped", 0, None

                size = self._raw_upload(file_path, dbx_path)
                return "uploaded", size, None
            except Exception as exc:
                return "failed", 0, f"{file_path}: {exc}"

        with ThreadPoolExecutor(max_workers=self._max_workers) as pool:
            futures = {pool.submit(_upload_if_changed, f): f for f in regular_files}
            for future in as_completed(futures):
                tally.record(*future.result())

        if log_path:
            if tally.failed:
                logger.warning("Skipping file_log.json upload because one or more content files failed.")
            else:
                tally.record(*_upload_if_changed(log_path))

        return tally.finish("uploaded", "Dropbox upload")

    def download_directory(self, remote_directory: str, local_directory: str) -> SyncResult:
        self._require_client()
        tally = SyncTally(time.time())

        for info in tally.listing(self.list_files, remote_directory):
            local_path = os.path.join(
                local_directory,
                info.remote_path[len(remote_directory):].lstrip("/"),
            )
            if not is_path_within(local_directory, local_path):
                tally.record("failed", error=f"{info.remote_path}: resolves outside {local_directory}")
                continue

            try:
                # Skip unchanged local files (compare Dropbox hashes)
                if os.path.exists(local_path):
                    local_hash = _dropbox_content_hash(local_path)
                    if local_hash == info.content_hash:
                        tally.record("skipped")
                        continue

                tally.record("downloaded", self._download_to(info.remote_path, local_path))
            except Exception as exc:
                tally.record("failed", error=f"{info.remote_path}: {exc}")

        return tally.finish("downloaded", "Dropbox download")

    def download_log(self, remote_directory: str, local_directory: str) -> SyncResult:
        self._require_client()
        start = time.time()
        log_local = os.path.join(local_directory, LOG_FILE_NAME)

        try:
            size = self._download_to(f"{remote_directory}/{LOG_FILE_NAME}", log_local)
        except Exception as exc:
            if isinstance(exc, ApiError) and _is_not_found(exc):
                logger.info("No existing log file in Dropbox — starting fresh.")
                return SyncResult(elapsed_seconds=time.time() - start)
            logger.error(f"Failed to download log file: {exc}")
            return SyncResult(failed=1, elapsed_seconds=time.time() - start, errors=[str(exc)])

        logger.info(f"Log file downloaded to {log_local}.")
        return SyncResult(downloaded=1, bytes_transferred=size, elapsed_seconds=time.time() - start)

    def get_provider_name(self) -> str:
        return "Dropbox"

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _require_client(self):
        if self._dbx is None:
            raise RuntimeError("Call connect() before using the Dropbox provider.")

    def _remote_hashes(self, remote_directory: str) -> dict[str, str | None]:
        """Dropbox hash per remote path, empty when the listing fails."""
        try:
            return {info.remote_path: info.content_hash for info in self.list_files(remote_directory)}
        except StorageError as err:
            # Hashes only skip unchanged files; uploading everything is still correct.
            logger.warning(f"{err} -- uploading all files without change detection.")
            return {}

    def _raw_upload(self, local_path: str, remote_path: str) -> int:
        """Upload a single file, using chunked upload for large files."""
        file_size = os.path.getsize(local_path)

        if file_size <= SINGLE_UPLOAD_LIMIT:
            with open(local_path, "rb") as f:
                self._dbx.files_upload(
                    f.read(), remote_path,
                    mode=dropbox.files.WriteMode.overwrite,
                )
        else:
            with open(local_path, "rb") as f:
                chunk = f.read(UPLOAD_CHUNK_SIZE)
                session = self._dbx.files_upload_session_start(chunk)
                cursor = dropbox.files.UploadSessionCursor(
                    session_id=session.session_id, offset=len(chunk),
                )
                commit = dropbox.files.CommitInfo(
                    path=remote_path,
                    mode=dropbox.files.WriteMode.overwrite,
                )
                while True:
                    chunk = f.read(UPLOAD_CHUNK_SIZE)
                    if f.tell() >= file_size:
                        self._dbx.files_upload_session_finish(chunk, cursor, commit)
                        break
                    self._dbx.files_upload_session_append_v2(chunk, cursor)
                    cursor.offset += len(chunk)

        return file_size

    def _download_to(self, remote_path: str, local_path: str) -> int:
        """Download one file to local_path, creating its folder; returns the size."""
        os.makedirs(os.path.dirname(local_path) or ".", exist_ok=True)
        meta, res = self._dbx.files_download(remote_path)
        with open(local_path, "wb") as f:
            f.write(res.content)
        return meta.size
