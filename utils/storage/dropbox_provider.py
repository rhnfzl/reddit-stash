"""Dropbox storage provider with resilient downloads."""

from __future__ import annotations

import configparser
import hashlib
import os
import re
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Optional

import requests

from utils.config_paths import get_settings_file_path
from utils.storage.base import StorageFileInfo, SyncResult
from utils.storage.content_hash import compute_file_hash

_dropbox = None
_ApiError = None
_FileMetadata = None

SINGLE_UPLOAD_LIMIT = 150 * 1024 * 1024
UPLOAD_CHUNK_SIZE = 4 * 1024 * 1024
DOWNLOAD_RETRIES = 4
DOWNLOAD_RETRY_DELAYS = (2, 5, 12, 25)


def _ensure_dropbox():
    global _dropbox, _ApiError, _FileMetadata
    if _dropbox is None:
        import dropbox
        from dropbox.exceptions import ApiError
        from dropbox.files import FileMetadata
        _dropbox = dropbox
        _ApiError = ApiError
        _FileMetadata = FileMetadata


class _DropboxContentHasher:
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
    hasher = _DropboxContentHasher()
    with open(file_path, "rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            hasher.update(chunk)
    return hasher.hexdigest()


def _sanitize_filename(name: str) -> str:
    sanitized = re.sub(r'[<>:"/\\|?*\x00-\x1F]', "_", name).strip()
    reserved = {
        "CON", "PRN", "AUX", "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }
    if sanitized.upper() in reserved:
        sanitized = "_" + sanitized
    return sanitized


def _read_bool(section: str, key: str, fallback: bool = False) -> bool:
    parser = configparser.ConfigParser()
    parser.read(get_settings_file_path())
    try:
        return parser.getboolean(section, key, fallback=fallback)
    except Exception:
        return fallback


def _process_gdpr_enabled() -> bool:
    return _read_bool("Settings", "process_gdpr", False)


def list_files_with_hashes(dbx, dropbox_folder_path: str) -> List[StorageFileInfo]:
    result_list = []
    try:
        result = dbx.files_list_folder(dropbox_folder_path, recursive=True)
        while True:
            for entry in result.entries:
                if isinstance(entry, _FileMetadata):
                    result_list.append(
                        StorageFileInfo(
                            remote_path=entry.path_lower,
                            content_hash=entry.content_hash,
                            size_bytes=entry.size,
                        )
                    )
            if not result.has_more:
                break
            result = dbx.files_list_folder_continue(result.cursor)
    except _ApiError as exc:
        # A confirmed path-not-found is the one legitimate empty-folder case
        # (for example, the first upload to a new remote root). All permission,
        # authentication, rate-limit and transport errors must propagate;
        # treating them as an empty archive could trigger duplicate uploads or
        # an incomplete restore.
        try:
            if exc.error.is_path() and exc.error.get_path().is_not_found():
                return []
        except Exception:
            pass
        raise RuntimeError(
            f"Failed to list Dropbox folder {dropbox_folder_path}: {exc}"
        ) from exc
    return result_list


def _download_with_retry(dbx, remote_path: str, local_path: str, retries: int = DOWNLOAD_RETRIES):
    """
    Retry transient Dropbox/network timeouts. Write to a temporary file so a partial
    response never becomes the local canonical file.
    """
    os.makedirs(os.path.dirname(local_path) or ".", exist_ok=True)
    last_error = None

    for attempt in range(retries):
        temp_path = f"{local_path}.part"
        try:
            try:
                os.remove(temp_path)
            except OSError:
                pass

            metadata, response = dbx.files_download(remote_path)
            with open(temp_path, "wb") as handle:
                handle.write(response.content)

            expected = int(getattr(metadata, "size", 0) or 0)
            actual = os.path.getsize(temp_path)
            if expected and actual != expected:
                raise IOError(
                    f"Dropbox download size mismatch for {remote_path}: "
                    f"expected {expected}, got {actual}"
                )

            os.replace(temp_path, local_path)
            return metadata, actual

        except Exception as exc:
            last_error = exc
            print(
                f"Dropbox download retry {attempt + 1}/{retries} failed for "
                f"{remote_path}: {exc}"
            )
            try:
                os.remove(temp_path)
            except OSError:
                pass

            if attempt + 1 < retries:
                time.sleep(DOWNLOAD_RETRY_DELAYS[min(attempt, len(DOWNLOAD_RETRY_DELAYS) - 1)])

    raise last_error if last_error else RuntimeError(f"Dropbox download failed: {remote_path}")


def _download_directory_tree(dbx, remote_directory: str, local_directory: str) -> SyncResult:
    start = time.time()
    remote_files = sorted(
        list_files_with_hashes(dbx, remote_directory),
        key=lambda info: info.remote_path,
    )
    downloaded = skipped = failed = bytes_transferred = 0
    errors: List[str] = []

    for info in remote_files:
        relative = info.remote_path[len(remote_directory):].lstrip("/")
        local_path = os.path.join(local_directory, relative)

        try:
            if os.path.exists(local_path):
                local_hash = _dropbox_content_hash(local_path)
                if local_hash == info.content_hash:
                    skipped += 1
                    continue

            _, size = _download_with_retry(dbx, info.remote_path, local_path)
            downloaded += 1
            bytes_transferred += size
        except Exception as exc:
            failed += 1
            errors.append(f"{info.remote_path}: {exc}")

    result = SyncResult(
        downloaded=downloaded,
        skipped=skipped,
        failed=failed,
        bytes_transferred=bytes_transferred,
        elapsed_seconds=time.time() - start,
        errors=errors,
    )
    print(f"Dropbox download: {result.summary()}")
    return result


class DropboxStorageProvider:
    def __init__(self, dropbox_directory: str = "/reddit"):
        self._dropbox_directory = dropbox_directory.rstrip("/") or "/"
        self._dbx = None
        self._max_workers = 3

    def connect(self) -> None:
        _ensure_dropbox()
        refresh_token = os.getenv("DROPBOX_REFRESH_TOKEN")
        client_id = os.getenv("DROPBOX_APP_KEY")
        client_secret = os.getenv("DROPBOX_APP_SECRET")
        if not all([refresh_token, client_id, client_secret]):
            raise RuntimeError(
                "Missing Dropbox credentials. Set DROPBOX_REFRESH_TOKEN, "
                "DROPBOX_APP_KEY, and DROPBOX_APP_SECRET."
            )

        response = requests.post(
            "https://api.dropboxapi.com/oauth2/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": client_id,
                "client_secret": client_secret,
            },
            timeout=30,
        )
        response.raise_for_status()
        token = response.json().get("access_token")
        if not token:
            raise RuntimeError("Dropbox token refresh returned no access token.")

        os.environ["DROPBOX_TOKEN"] = token

        # Give long file transfers a larger SDK-level timeout. Individual
        # operations are still retried below.
        self._dbx = _dropbox.Dropbox(token, timeout=300)
        print(" -- Dropbox Access Token Refreshed -- ")

    def _require_client(self):
        if self._dbx is None:
            raise RuntimeError("Call connect() before using Dropbox.")

    def upload_file(self, local_path: str, remote_path: str) -> StorageFileInfo:
        self._require_client()
        size = self._raw_upload(local_path, remote_path)
        return StorageFileInfo(
            remote_path=remote_path,
            content_hash=compute_file_hash(local_path),
            size_bytes=size,
        )

    def _raw_upload(self, local_path: str, remote_path: str) -> int:
        file_size = os.path.getsize(local_path)

        if file_size <= SINGLE_UPLOAD_LIMIT:
            with open(local_path, "rb") as handle:
                self._dbx.files_upload(
                    handle.read(),
                    remote_path,
                    mode=_dropbox.files.WriteMode.overwrite,
                )
            return file_size

        with open(local_path, "rb") as handle:
            first = handle.read(UPLOAD_CHUNK_SIZE)
            session = self._dbx.files_upload_session_start(first)
            cursor = _dropbox.files.UploadSessionCursor(
                session_id=session.session_id,
                offset=len(first),
            )
            commit = _dropbox.files.CommitInfo(
                path=remote_path,
                mode=_dropbox.files.WriteMode.overwrite,
            )

            while True:
                chunk = handle.read(UPLOAD_CHUNK_SIZE)
                if handle.tell() >= file_size:
                    self._dbx.files_upload_session_finish(chunk, cursor, commit)
                    break
                self._dbx.files_upload_session_append_v2(chunk, cursor)
                cursor.offset += len(chunk)

        return file_size

    def download_file(self, remote_path: str, local_path: str) -> StorageFileInfo:
        self._require_client()
        metadata, size = _download_with_retry(self._dbx, remote_path, local_path)
        return StorageFileInfo(
            remote_path=remote_path,
            content_hash=compute_file_hash(local_path),
            size_bytes=size or getattr(metadata, "size", 0),
        )

    def list_files(self, remote_directory: str) -> List[StorageFileInfo]:
        self._require_client()
        return list_files_with_hashes(self._dbx, remote_directory)

    def get_file_info(self, remote_path: str) -> Optional[StorageFileInfo]:
        self._require_client()
        try:
            metadata = self._dbx.files_get_metadata(remote_path)
            if isinstance(metadata, _FileMetadata):
                return StorageFileInfo(
                    remote_path=metadata.path_lower,
                    content_hash=metadata.content_hash,
                    size_bytes=metadata.size,
                )
        except _ApiError as exc:
            # Only a true path-not-found response means the file is absent.
            # Permission, authentication and transport errors must propagate.
            try:
                if exc.error.is_path() and exc.error.get_path().is_not_found():
                    return None
            except Exception:
                pass
            raise RuntimeError(f"Dropbox metadata lookup failed for {remote_path}: {exc}") from exc
        return None

    def file_exists(self, remote_path: str) -> bool:
        return self.get_file_info(remote_path) is not None

    def upload_directory(
        self,
        local_directory: str,
        remote_directory: str,
        check_type: str = "DIR",
    ) -> SyncResult:
        self._require_client()
        start = time.time()
        remote_hashes = {
            info.remote_path.lower(): info.content_hash
            for info in self.list_files(remote_directory)
        }

        files = []
        for root, _, names in os.walk(local_directory):
            for name in sorted(names):
                if name.startswith("."):
                    continue
                files.append((root, name))
        files.sort()

        uploaded = skipped = failed = bytes_transferred = 0
        errors = []

        def process(item):
            root, name = item
            path = os.path.join(root, name)
            rel = os.path.relpath(path, local_directory).replace(os.sep, "/")
            remote = f"{remote_directory.rstrip('/')}/" + "/".join(
                _sanitize_filename(part) for part in rel.split("/")
            )

            if remote.lower() in remote_hashes:
                if _dropbox_content_hash(path) == remote_hashes[remote.lower()]:
                    return ("skip", 0, None)

            try:
                size = self._raw_upload(path, remote)
                return ("upload", size, None)
            except Exception as exc:
                return ("fail", 0, f"{path}: {exc}")

        with ThreadPoolExecutor(max_workers=self._max_workers) as pool:
            futures = [pool.submit(process, item) for item in files]
            for future in as_completed(futures):
                status, size, error = future.result()
                if status == "skip":
                    skipped += 1
                elif status == "upload":
                    uploaded += 1
                    bytes_transferred += size
                else:
                    failed += 1
                    errors.append(error)

        result = SyncResult(
            uploaded=uploaded,
            skipped=skipped,
            failed=failed,
            bytes_transferred=bytes_transferred,
            elapsed_seconds=time.time() - start,
            errors=errors,
        )
        print(f"Dropbox upload: {result.summary()}")
        return result

    def download_directory(
        self,
        remote_directory: str,
        local_directory: str,
        check_type: str = "DIR",
    ) -> SyncResult:
        self._require_client()
        if check_type.upper() == "LOG":
            start = time.time()
            local_log = os.path.join(local_directory, "file_log.json")
            remote_log = f"{remote_directory.rstrip('/')}/file_log.json"
            try:
                _, size = _download_with_retry(self._dbx, remote_log, local_log)
                result = SyncResult(
                    downloaded=1,
                    bytes_transferred=size,
                    elapsed_seconds=time.time() - start,
                )
                print(f"Dropbox log download: {result.summary()}")
                return result
            except _ApiError as exc:
                try:
                    if exc.error.is_path() and exc.error.get_path().is_not_found():
                        print("No existing log file in Dropbox — starting fresh.")
                        return SyncResult(elapsed_seconds=time.time() - start)
                except Exception:
                    pass
                return SyncResult(
                    failed=1,
                    elapsed_seconds=time.time() - start,
                    errors=[str(exc)],
                )
            except Exception as exc:
                return SyncResult(
                    failed=1,
                    elapsed_seconds=time.time() - start,
                    errors=[str(exc)],
                )

        return _download_directory_tree(self._dbx, remote_directory, local_directory)

    def get_provider_name(self) -> str:
        return "Dropbox"


__all__ = ["DropboxStorageProvider"]
