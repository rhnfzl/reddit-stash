"""Per-file and per-day media size enforcement shared by all download paths."""
from __future__ import annotations

import configparser
import os
import sqlite3
import tempfile
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from utils.config_paths import get_settings_file_path
from utils.feature_flags import get_media_config


class MediaLimitExceeded(RuntimeError):
    pass


_cache_lock = threading.Lock()
_quota_cache: dict[tuple[str, int], "DailyMediaQuota"] = {}


def archive_root() -> Path:
    parser = configparser.ConfigParser()
    parser.read(get_settings_file_path())
    configured = parser.get("Settings", "save_directory", fallback="reddit_dropbox/")
    path = Path(configured).expanduser()
    return path.resolve() if path.is_absolute() else (Path.cwd() / path).resolve()


def media_size_limit(url: str = "", content_type: str = "") -> int:
    config = get_media_config()
    is_video = content_type.lower().startswith(("video/", "audio/"))
    if not is_video:
        path = urlparse(url).path.lower()
        is_video = path.endswith((".mp4", ".webm", ".mov", ".mkv", ".m4v", ".mp3", ".m4a", ".mpd", ".m3u8"))
    return config.max_video_size() if is_video else config.max_image_size()


class DailyMediaQuota:
    """SQLite-backed quota safe across threads and independent processes."""
    def __init__(self, root: Path, limit_bytes: int):
        self.root = root
        self.limit_bytes = max(0, int(limit_bytes))
        self.db_path = root / "media_usage.sqlite3"
        self._init_lock = threading.Lock()
        self._ensure_database()

    def _connect(self):
        self.root.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path), timeout=30)
        conn.execute("PRAGMA busy_timeout = 30000")
        return conn

    @contextmanager
    def session(self):
        """Yield a transactional SQLite connection and always close it."""
        conn = self._connect()
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _ensure_database(self):
        with self._init_lock, self.session() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS daily_usage (day TEXT PRIMARY KEY, used_bytes INTEGER NOT NULL)")

    @staticmethod
    def _today() -> str:
        return datetime.now(timezone.utc).date().isoformat()

    def reserve(self, amount: int) -> bool:
        amount = int(amount)
        if amount <= 0:
            return True
        with self.session() as conn:
            conn.execute("BEGIN IMMEDIATE")
            day = self._today()
            conn.execute("INSERT OR IGNORE INTO daily_usage(day, used_bytes) VALUES (?, 0)", (day,))
            used = conn.execute("SELECT used_bytes FROM daily_usage WHERE day=?", (day,)).fetchone()[0]
            if used + amount > self.limit_bytes:
                conn.rollback()
                return False
            conn.execute("UPDATE daily_usage SET used_bytes=used_bytes+? WHERE day=?", (amount, day))
            conn.commit()
            return True

    def release(self, amount: int) -> None:
        amount = max(0, int(amount))
        if not amount:
            return
        with self.session() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "UPDATE daily_usage SET used_bytes=MAX(0, used_bytes-?) WHERE day=?",
                (amount, self._today()),
            )
            conn.commit()


def get_daily_quota() -> DailyMediaQuota:
    config = get_media_config()
    root = archive_root()
    limit_bytes = config.max_daily_storage_mb() * 1024 * 1024
    key = (str(root), limit_bytes)
    with _cache_lock:
        quota = _quota_cache.get(key)
        if quota is None:
            quota = DailyMediaQuota(root, limit_bytes)
            _quota_cache[key] = quota
        return quota


def file_signature(path: str) -> tuple[int, int] | None:
    """Return (size, mtime_ns), or None when a path does not exist.

    yt-dlp can report an existing output as a successful download. Comparing
    signatures before and after extraction prevents charging the daily transfer
    quota again for an unchanged archive file.
    """
    try:
        stat = Path(path).stat()
        return stat.st_size, stat.st_mtime_ns
    except OSError:
        return None


def register_downloaded_file(path: str, max_size: int) -> int:
    """Account for a completed yt-dlp output or delete it if it exceeds limits."""
    file_path = Path(path)
    size = file_path.stat().st_size
    if size <= 0:
        file_path.unlink(missing_ok=True)
        raise MediaLimitExceeded("Downloaded media file is empty")
    if max_size and size > max_size:
        file_path.unlink(missing_ok=True)
        raise MediaLimitExceeded(f"Downloaded file is {size} bytes; per-file limit is {max_size}")
    if not get_daily_quota().reserve(size):
        file_path.unlink(missing_ok=True)
        raise MediaLimitExceeded("Daily media-storage quota has been reached")
    return size


def stream_response_to_file(response, save_path: str, max_size: int) -> int:
    """Stream atomically, enforcing per-file and per-UTC-day transfer limits."""
    destination = Path(save_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        declared = int(response.headers.get("Content-Length", "0") or 0)
    except (TypeError, ValueError):
        declared = 0
    if declared > 0 and max_size > 0 and declared > max_size:
        raise MediaLimitExceeded(f"Content-Length {declared} exceeds per-file limit {max_size}")

    quota = get_daily_quota()
    fd, temp_name = tempfile.mkstemp(prefix=destination.name + ".", suffix=".part", dir=str(destination.parent))
    reserved = 0
    written = 0
    committed = False
    try:
        with os.fdopen(fd, "wb") as handle:
            for chunk in response.iter_content(chunk_size=256 * 1024):
                if not chunk:
                    continue
                next_size = written + len(chunk)
                if max_size > 0 and next_size > max_size:
                    raise MediaLimitExceeded(f"File exceeds configured limit of {max_size} bytes")
                if not quota.reserve(len(chunk)):
                    raise MediaLimitExceeded("Daily media-storage quota has been reached")
                reserved += len(chunk)
                handle.write(chunk)
                written = next_size
            handle.flush()
            os.fsync(handle.fileno())
        if written <= 0:
            raise MediaLimitExceeded("Download completed without any content")
        os.replace(temp_name, destination)
        committed = True
        # This is a transfer quota, not a current-directory-size quota. Do not
        # credit the previous destination size: it may have been downloaded on
        # a different day and must not reduce today's usage.
        return written
    except Exception:
        if not committed:
            try:
                os.unlink(temp_name)
            except OSError:
                pass
            quota.release(reserved)
        # Once os.replace succeeds, the bytes are committed and remain charged.
        raise
