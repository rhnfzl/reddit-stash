"""Dropbox storage provider used by storage_utils.py.

The provider supports recursive uploads/downloads and the repository's LOG
mode. Uploads create every missing remote folder before starting concurrent
file uploads; Dropbox's upload endpoints do not create missing parent folders.
"""
from __future__ import annotations

import configparser
import hashlib
import os
import posixpath
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Optional

from utils.config_paths import get_settings_file_path
from utils.storage.base import StorageFileInfo, SyncResult
from utils.storage.content_hash import compute_file_hash

# Lazy import so local-only installations do not need the Dropbox SDK.
_dropbox = None
_ApiError = None
_FileMetadata = None
_FolderMetadata = None

SINGLE_UPLOAD_LIMIT = 150 * 1024 * 1024  # Dropbox single-upload limit: 150 MB.
UPLOAD_CHUNK_SIZE = 4 * 1024 * 1024


def _ensure_dropbox() -> None:
    """Import the Dropbox SDK only when Dropbox is actually selected."""
    global _dropbox, _ApiError, _FileMetadata, _FolderMetadata
    if _dropbox is None:
        import dropbox
        from dropbox.exceptions import ApiError
        from dropbox.files import FileMetadata, FolderMetadata

        _dropbox = dropbox
        _ApiError = ApiError
        _FileMetadata = FileMetadata
        _FolderMetadata = FolderMetadata


class _DropboxContentHasher:
    """Implement Dropbox's content hash (SHA-256 over 4 MiB block hashes)."""

    BLOCK_SIZE = 4 * 1024 * 1024

    def __init__(self) -> None:
        self._overall = hashlib.sha256()
        self._block = hashlib.sha256()
        self._block_pos = 0
        self._finished = False

    def update(self, data: bytes) -> None:
        if self._finished:
            raise RuntimeError("Cannot update a finalized Dropbox content hash")

        pos = 0
        while pos < len(data):
            if self._block_pos == self.BLOCK_SIZE:
                self._overall.update(self._block.digest())
                self._block = hashlib.sha256()
                self._block_pos = 0

            available = self.BLOCK_SIZE - self._block_pos
            part = data[pos : pos + available]
            self._block.update(part)
            self._block_pos += len(part)
            pos += len(part)

    def hexdigest(self) -> str:
        if not self._finished:
            if self._block_pos > 0:
                self._overall.update(self._block.digest())
            self._finished = True
        return self._overall.hexdigest()


def _dropbox_content_hash(file_path: str) -> str:
    """Return the Dropbox-specific hash of a local file."""
    hasher = _DropboxContentHasher()
    with open(file_path, "rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            hasher.update(chunk)
    return hasher.hexdigest()


def _normalize_dropbox_path(path: str | None) -> str:
    """Normalize a Dropbox path to '' (root) or an absolute slash path."""
    value = (path or "").strip().replace("\\", "/")
    parts = [part for part in value.split("/") if part]
    return "/" + "/".join(parts) if parts else ""


def _join_dropbox_path(root: str, relative_path: str) -> str:
    root = _normalize_dropbox_path(root)
    relative = relative_path.strip("/")
    if not relative:
        return root
    return f"{root}/{relative}" if root else f"/{relative}"


def _sanitize_filename(name: str) -> str:
    """Replace characters that Dropbox does not allow in path components."""
    sanitized = re.sub(r'[<>:"/\\|?*\x00-\x1F]', "_", name).strip()
    reserved = {
        "CON", "PRN", "AUX", "NUL",
        "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
        "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9",
    }
    if sanitized.upper() in reserved:
        sanitized = "_" + sanitized
    return sanitized or "_"


def _api_error_is_path_not_found(exc: Exception) -> bool:
    """Return True only for Dropbox's explicit path/not_found error."""
    error = getattr(exc, "error", None)
    if error is None:
        return False
    try:
        return bool(error.is_path() and error.get_path().is_not_found())
    except (AttributeError, TypeError, ValueError):
        return False


def _read_bool_from_settings(section: str, key: str, fallback: bool = False) -> bool:
    parser = configparser.ConfigParser()
    parser.read(get_settings_file_path())
    if parser.has_option(section, key):
        try:
            return parser.getboolean(section, key)
        except (ValueError, configparser.Error):
            pass
    return fallback


def _process_gdpr_enabled() -> bool:
    return _read_bool_from_settings("Settings", "process_gdpr", fallback=False)


def list_files_with_hashes(dbx, dropbox_folder_path: str) -> List[StorageFileInfo]:
    """List files recursively. Only a confirmed missing folder is treated as empty.

    Authentication, permission, rate-limit, and transport/API failures must not
    be converted into an empty result: doing so makes an upload look like a
    first-time backup and can hide a broken connection.
    """
    _ensure_dropbox()
    folder_path = _normalize_dropbox_path(dropbox_folder_path)
    result_list: List[StorageFileInfo] = []

    try:
        result = dbx.files_list_folder(folder_path, recursive=True)
        while True:
            for entry in result.entries:
                if isinstance(entry, _FileMetadata):
                    result_list.append(
                        StorageFileInfo(
                            # path_display preserves the user's original case;
                            # path_lower is used separately for comparisons.
                            remote_path=getattr(entry, "path_display", None) or entry.path_lower,
                            content_hash=entry.content_hash,
                            size_bytes=entry.size,
                        )
                    )

            if not result.has_more:
                break
            result = dbx.files_list_folder_continue(result.cursor)

    except _ApiError as exc:
        if _api_error_is_path_not_found(exc):
            return []
        raise RuntimeError(
            f"Failed to list Dropbox folder {folder_path or '/'}: {exc}"
        ) from exc

    return result_list


def _download_directory_tree(dbx, remote_directory: str, local_directory: str) -> SyncResult:
    """Download a Dropbox directory recursively, preserving relative paths."""
    start = time.time()
    remote_root = _normalize_dropbox_path(remote_directory)
    remote_files = sorted(
        list_files_with_hashes(dbx, remote_root),
        key=lambda info: info.remote_path.casefold(),
    )

    downloaded = skipped = failed = bytes_transferred = 0
    errors: List[str] = []

    for info in remote_files:
        remote_path = info.remote_path
        remote_key = remote_path.casefold()
        root_key = remote_root.casefold()

        if root_key:
            prefix = root_key.rstrip("/") + "/"
            if not remote_key.startswith(prefix):
                # Ignore any unexpected entry outside the requested root.
                continue
            relative = remote_path[len(remote_root.rstrip("/")) :].lstrip("/")
        else:
            relative = remote_path.lstrip("/")

        relative_parts = relative.split("/")
        if not relative or any(part in {"", ".", ".."} for part in relative_parts):
            failed += 1
            errors.append(f"Unsafe Dropbox relative path: {remote_path}")
            continue

        local_path = os.path.join(local_directory, *relative_parts)
        try:
            if os.path.isfile(local_path):
                local_hash = _dropbox_content_hash(local_path)
                if local_hash == info.content_hash:
                    skipped += 1
                    continue

            parent = os.path.dirname(local_path)
            if parent:
                os.makedirs(parent, exist_ok=True)

            metadata, response = dbx.files_download(remote_path)
            with open(local_path, "wb") as handle:
                handle.write(response.content)

            downloaded += 1
            bytes_transferred += getattr(metadata, "size", len(response.content))
        except Exception as exc:
            failed += 1
            errors.append(f"{remote_path}: {exc}")

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
    """Dropbox implementation of the storage provider protocol."""

    def __init__(self, dropbox_directory: str = "/reddit") -> None:
        self._dropbox_directory = _normalize_dropbox_path(dropbox_directory)
        self._dbx = None
        self._max_workers = 3

    # ------------------------------------------------------------------
    # Connection and protocol methods
    # ------------------------------------------------------------------
    def connect(self) -> None:
        """Refresh the OAuth token and create a Dropbox SDK client."""
        _ensure_dropbox()
        import requests

        refresh_token = os.getenv("DROPBOX_REFRESH_TOKEN")
        client_id = os.getenv("DROPBOX_APP_KEY")
        client_secret = os.getenv("DROPBOX_APP_SECRET")
        if not all((refresh_token, client_id, client_secret)):
            raise RuntimeError(
                "Missing Dropbox credentials. Set DROPBOX_REFRESH_TOKEN, "
                "DROPBOX_APP_KEY, and DROPBOX_APP_SECRET environment variables."
            )

        try:
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
        except Exception as exc:
            raise RuntimeError(f"Failed to refresh Dropbox access token: {exc}") from exc

        if not token:
            raise RuntimeError("Dropbox token refresh returned no access_token.")

        os.environ["DROPBOX_TOKEN"] = token
        self._dbx = _dropbox.Dropbox(token)
        print(" -- Dropbox Access Token Refreshed -- ")

    def upload_file(self, local_path: str, remote_path: str) -> StorageFileInfo:
        self._require_client()
        if not os.path.isfile(local_path):
            raise FileNotFoundError(f"Local upload file does not exist: {local_path}")

        remote_path = _normalize_dropbox_path(remote_path)
        parent = posixpath.dirname(remote_path)
        self._ensure_remote_folder(parent)
        size = self._raw_upload(local_path, remote_path)
        return StorageFileInfo(
            remote_path=remote_path,
            content_hash=compute_file_hash(local_path),
            size_bytes=size,
        )

    def download_file(self, remote_path: str, local_path: str) -> StorageFileInfo:
        self._require_client()
        remote_path = _normalize_dropbox_path(remote_path)
        parent = os.path.dirname(local_path)
        if parent:
            os.makedirs(parent, exist_ok=True)

        metadata, response = self._dbx.files_download(remote_path)
        with open(local_path, "wb") as handle:
            handle.write(response.content)

        return StorageFileInfo(
            remote_path=remote_path,
            content_hash=compute_file_hash(local_path),
            size_bytes=metadata.size,
        )

    def list_files(self, remote_directory: str) -> List[StorageFileInfo]:
        self._require_client()
        return list_files_with_hashes(self._dbx, remote_directory)

    def get_file_info(self, remote_path: str) -> Optional[StorageFileInfo]:
        self._require_client()
        remote_path = _normalize_dropbox_path(remote_path)
        try:
            metadata = self._dbx.files_get_metadata(remote_path)
            if isinstance(metadata, _FileMetadata):
                return StorageFileInfo(
                    remote_path=getattr(metadata, "path_display", None) or metadata.path_lower,
                    content_hash=metadata.content_hash,
                    size_bytes=metadata.size,
                )
            return None
        except _ApiError as exc:
            if _api_error_is_path_not_found(exc):
                return None
            raise RuntimeError(f"Failed to read Dropbox metadata for {remote_path}: {exc}") from exc

    def file_exists(self, remote_path: str) -> bool:
        return self.get_file_info(remote_path) is not None

    def upload_directory(
        self,
        local_directory: str,
        remote_directory: str,
        check_type: str = "DIR",
    ) -> SyncResult:
        """Upload a local tree, creating the remote tree before file uploads.

        Creating the folders up front avoids the common failure where a
        Markdown file in an existing subreddit folder uploads but its media
        file fails because its new per-post folder does not yet exist.
        """
        self._require_client()
        start = time.time()
        local_directory = os.path.abspath(local_directory)
        if not os.path.isdir(local_directory):
            raise NotADirectoryError(f"Local upload directory does not exist: {local_directory}")

        remote_root = _normalize_dropbox_path(remote_directory or self._dropbox_directory)

        # A listing error must stop the upload; it must never masquerade as an
        # empty remote archive.
        remote_files = self.list_files(remote_root)
        remote_hashes = {
            info.remote_path.casefold(): info.content_hash
            for info in remote_files
        }

        files_to_upload: List[tuple[str, str]] = []
        folders_to_create = set()
        if remote_root:
            folders_to_create.add(remote_root)

        for root, dirs, files in os.walk(local_directory):
            dirs[:] = sorted(directory for directory in dirs if not directory.startswith("."))
            for filename in sorted(files):
                if filename.startswith("."):
                    continue
                local_path = os.path.join(root, filename)
                relative = os.path.relpath(local_path, local_directory).replace(os.sep, "/")
                sanitized_parts = [_sanitize_filename(part) for part in relative.split("/")]
                if not sanitized_parts or any(part in {"", ".", ".."} for part in sanitized_parts):
                    continue

                files_to_upload.append((local_path, "/".join(sanitized_parts)))
                for depth in range(1, len(sanitized_parts)):
                    folder_relative = "/".join(sanitized_parts[:depth])
                    folders_to_create.add(_join_dropbox_path(remote_root, folder_relative))

        # Ensure every ancestor exists, including ancestors of the configured
        # remote root, before worker threads begin uploading files.
        for folder in sorted(folders_to_create, key=lambda value: (value.count("/"), value.casefold())):
            self._ensure_remote_folder(folder)

        uploaded = skipped = failed = bytes_transferred = 0
        errors: List[str] = []
        lock = threading.Lock()

        def _process(file_item: tuple[str, str]) -> None:
            nonlocal uploaded, skipped, failed, bytes_transferred
            local_path, sanitized_relative = file_item
            remote_path = _join_dropbox_path(remote_root, sanitized_relative)

            try:
                local_hash = _dropbox_content_hash(local_path)
                if remote_hashes.get(remote_path.casefold()) == local_hash:
                    with lock:
                        skipped += 1
                    return

                size = self._raw_upload(local_path, remote_path)
                with lock:
                    uploaded += 1
                    bytes_transferred += size
            except Exception as exc:
                with lock:
                    failed += 1
                    errors.append(f"{local_path} -> {remote_path}: {exc}")

        with ThreadPoolExecutor(max_workers=self._max_workers) as pool:
            futures = [pool.submit(_process, item) for item in files_to_upload]
            for future in as_completed(futures):
                # _process captures per-file errors and updates SyncResult.
                future.result()

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
        remote_root = _normalize_dropbox_path(remote_directory or self._dropbox_directory)
        start = time.time()

        if check_type.upper() == "LOG":
            log_result = self._download_log_only(remote_root, local_directory, start)
            if _process_gdpr_enabled():
                gdpr_result = self._download_gdpr_data(remote_root, local_directory)
                return self._merge_results(log_result, gdpr_result)
            return log_result

        return _download_directory_tree(self._dbx, remote_root, local_directory)

    def get_provider_name(self) -> str:
        return "Dropbox"

    # ------------------------------------------------------------------
    # Dropbox-specific helpers
    # ------------------------------------------------------------------
    def _require_client(self) -> None:
        if self._dbx is None:
            raise RuntimeError("Call connect() before using the Dropbox provider.")

    def _ensure_remote_folder(self, folder_path: str) -> None:
        """Create a folder and its ancestors; accept only an existing folder.

        Dropbox returns a conflict when a folder already exists. Rather than
        swallowing all API errors, verify the existing path is actually a
        FolderMetadata object. A file with the same name, permission failure,
        or network problem remains a visible failure.
        """
        folder_path = _normalize_dropbox_path(folder_path)
        if not folder_path:
            return  # Dropbox root always exists.

        parts = [part for part in folder_path.split("/") if part]
        for depth in range(1, len(parts) + 1):
            current = "/" + "/".join(parts[:depth])
            try:
                self._dbx.files_create_folder_v2(current, autorename=False)
            except _ApiError as create_error:
                try:
                    existing = self._dbx.files_get_metadata(current)
                except Exception as metadata_error:
                    raise RuntimeError(
                        f"Could not create or verify Dropbox folder {current}: {create_error}"
                    ) from metadata_error

                if not isinstance(existing, _FolderMetadata):
                    raise RuntimeError(
                        f"Dropbox path already exists but is not a folder: {current}"
                    ) from create_error

    def _raw_upload(self, local_path: str, remote_path: str) -> int:
        """Upload one file, using a Dropbox upload session above 150 MB."""
        remote_path = _normalize_dropbox_path(remote_path)
        file_size = os.path.getsize(local_path)

        if file_size <= SINGLE_UPLOAD_LI