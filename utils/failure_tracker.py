"""Persistent journal of Reddit items that failed to archive.

The scraper already retries uncaptured items by fetching the user's Reddit
listings on the next run. This journal makes failures auditable across runs;
it is not used as the source of truth for deduplication.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .archive_index import canonical_item_key

_FAILURES_FILE = "failed_items.json"
_lock = threading.RLock()


def _path(archive_dir: str | os.PathLike[str]) -> Path:
    return Path(archive_dir).expanduser().resolve() / _FAILURES_FILE


def _load(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Cannot trust failure journal {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"Failure journal {path} must contain a JSON object")
    return payload


def _atomic_write(path: Path, payload: dict[str, dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except Exception:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def record_failure(
    archive_dir: str | os.PathLike[str],
    item_id: str,
    subreddit: str,
    category: str,
    error: str,
) -> None:
    key = canonical_item_key(item_id, subreddit, category)
    path = _path(archive_dir)
    now = datetime.now(timezone.utc).isoformat()
    with _lock:
        payload = _load(path)
        previous = payload.get(key, {})
        attempts = int(previous.get("attempts", 0)) + 1 if isinstance(previous, dict) else 1
        payload[key] = {
            "id": str(item_id),
            "subreddit": str(subreddit or "unknown"),
            "category": str(category).upper(),
            "attempts": attempts,
            "last_failed_at": now,
            "last_error": str(error or "Unknown error")[:4000],
        }
        _atomic_write(path, payload)


def clear_failure(
    archive_dir: str | os.PathLike[str],
    item_id: str,
    subreddit: str,
    category: str,
) -> None:
    key = canonical_item_key(item_id, subreddit, category)
    path = _path(archive_dir)
    with _lock:
        payload = _load(path)
        if key in payload:
            del payload[key]
            _atomic_write(path, payload)


def load_failures(archive_dir: str | os.PathLike[str]) -> dict[str, dict[str, Any]]:
    """Return the current failure journal (for reports and support tooling)."""
    with _lock:
        return _load(_path(archive_dir))
