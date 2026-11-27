"""Atomic, thread-safe file-log persistence.

A corrupt log is never silently replaced with an empty dictionary. Doing that
would make a backup run forget what it had already archived.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any

try:
    from utils.constants import FILE_LOG_CHECKPOINT_INTERVAL
except Exception:  # pragma: no cover - defensive fallback for standalone tools
    FILE_LOG_CHECKPOINT_INTERVAL = 100

_log_lock = threading.RLock()


class FileLogError(RuntimeError):
    """Base class for unsafe file-log states."""


class CorruptFileLogError(FileLogError):
    """Raised when file_log.json exists but cannot be trusted."""


class MissingFileLogError(FileLogError):
    """Raised when the archive exists but its dedupe log is missing."""


def get_log_file_path(save_directory: str | os.PathLike[str]) -> str:
    return str(Path(save_directory) / "file_log.json")


def _contains_archived_markdown(save_directory: str | os.PathLike[str]) -> bool:
    root = Path(save_directory)
    if not root.exists():
        return False
    for path in root.rglob("*.md"):
        if path.is_file() and not path.name.startswith("."):
            return True
    return False


def load_file_log(
    save_directory: str | os.PathLike[str],
    *,
    allow_missing_with_archive: bool = False,
) -> dict:
    """Load a log or fail closed if existing archive data would be forgotten."""
    log_path = Path(get_log_file_path(save_directory))
    if not log_path.exists():
        if _contains_archived_markdown(save_directory) and not allow_missing_with_archive:
            raise MissingFileLogError(
                f"{log_path} is missing although Markdown archive files exist. "
                "Rebuild it with `python rebuild_file_log.py --archive "
                f"{str(save_directory)} --apply` before using check_type=LOG."
            )
        return {}

    try:
        with log_path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (json.JSONDecodeError, OSError) as exc:
        raise CorruptFileLogError(
            f"Cannot trust {log_path}: {exc}. The file was left untouched. "
            "Restore it from backup or rebuild it from the Markdown archive."
        ) from exc

    if not isinstance(data, dict):
        raise CorruptFileLogError(
            f"{log_path} must contain a JSON object; found {type(data).__name__}. "
            "The file was left untouched."
        )
    return data


def save_file_log(log_data: dict, save_directory: str | os.PathLike[str]) -> None:
    if not isinstance(log_data, dict):
        raise TypeError("log_data must be a dictionary")
    directory = Path(save_directory)
    directory.mkdir(parents=True, exist_ok=True)
    destination = Path(get_log_file_path(directory))

    with _log_lock:
        fd, temporary_path = tempfile.mkstemp(
            prefix="file_log.", suffix=".tmp", dir=str(directory)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(log_data, handle, indent=2, ensure_ascii=False, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, destination)
            # Best-effort directory fsync makes the rename durable on POSIX.
            try:
                dir_fd = os.open(str(directory), os.O_RDONLY)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
            except OSError:
                pass
        except Exception:
            try:
                os.unlink(temporary_path)
            except OSError:
                pass
            raise


def is_file_logged(log_data: dict, unique_key: str) -> bool:
    return unique_key in log_data


def log_file(log_data: dict, unique_key: str, file_info: dict, save_directory: str) -> None:
    """Thread-safe log mutation with periodic atomic checkpoints."""
    with _log_lock:
        info = dict(file_info)
        file_path = info.get("file_path")
        if file_path:
            try:
                info["file_path"] = os.path.relpath(file_path, start=save_directory)
            except ValueError:
                info["file_path"] = str(file_path)
        log_data[unique_key] = info
        interval = int(FILE_LOG_CHECKPOINT_INTERVAL or 0)
        if interval > 0 and len(log_data) % interval == 0:
            save_file_log(log_data, save_directory)
