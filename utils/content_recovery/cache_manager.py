"""
SQLite-based cache manager for content recovery system.

This module handles caching of recovery results to avoid repeated API calls
and improve performance. The retry queue itself lives in reddit_stash_retry_queue.db.
"""

import json
import time
import hashlib
import sqlite3
import logging
import threading
from typing import Any
from datetime import timedelta

from utils.content_recovery.recovery_metadata import RecoveryCacheEntry, RecoveryAttempt, RecoverySource, RecoveryQuality
from utils.content_recovery.recovery_config import load_cache_config
from utils.sqlite_manager import get_sqlite_manager


# A retained working directory may hold this file, so the name stays although the retry queue moved out of it.
RECOVERY_CACHE_DB_FILE = 'retry_queue.db'

_CREATE_TABLES = (
    """
    CREATE TABLE IF NOT EXISTS recovery_attempts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        original_url TEXT NOT NULL,
        recovery_source TEXT NOT NULL,
        attempted_at REAL NOT NULL,
        success INTEGER NOT NULL,
        recovered_url TEXT,
        error_message TEXT,
        duration_seconds REAL,
        UNIQUE(original_url, recovery_source, attempted_at)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS recovery_cache (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        url_hash TEXT NOT NULL,
        original_url TEXT NOT NULL,
        recovery_source TEXT NOT NULL,
        recovered_url TEXT,
        content_quality TEXT DEFAULT 'medium_quality',
        cached_at REAL NOT NULL,
        last_accessed_at REAL NOT NULL,
        expires_at REAL NOT NULL,
        metadata_json TEXT,
        success INTEGER NOT NULL DEFAULT 1,
        UNIQUE(url_hash, recovery_source)
    )
    """,
)

# Columns added after the first release, for databases created before them.
_ADDED_CACHE_COLUMNS = {
    'last_accessed_at': 'REAL',
    'success': 'INTEGER NOT NULL DEFAULT 1',
}

_INDEXES = {
    'idx_recovery_cache_lookup': 'recovery_cache(url_hash, recovery_source, expires_at)',
    'idx_recovery_cache_url_hash': 'recovery_cache(url_hash)',
    'idx_recovery_cache_expires': 'recovery_cache(expires_at)',
    'idx_recovery_cache_source': 'recovery_cache(recovery_source)',
    'idx_recovery_cache_cached_at': 'recovery_cache(cached_at)',
    'idx_recovery_cache_last_accessed': 'recovery_cache(last_accessed_at)',
    'idx_recovery_attempts_url': 'recovery_attempts(original_url)',
    'idx_recovery_attempts_source': 'recovery_attempts(recovery_source)',
    'idx_recovery_attempts_time': 'recovery_attempts(attempted_at)',
    'idx_recovery_attempts_stats': 'recovery_attempts(recovery_source, attempted_at, success)',
}


class RecoveryCacheManager:
    """Manages SQLite-based caching for content recovery results."""

    def __init__(self, db_path: str | None = None):
        self.db_path = db_path or RECOVERY_CACHE_DB_FILE
        self._logger = logging.getLogger(f"{__name__}.{self.__class__.__name__}")
        self.config = load_cache_config()
        self._lock = threading.Lock()
        self._last_cleanup: float | None = None
        self.sqlite_manager = get_sqlite_manager(self.db_path)
        self._ensure_tables()

    def _ensure_tables(self):
        """Create recovery tables if they don't exist."""
        try:
            with self.sqlite_manager.get_connection() as conn:
                cursor = conn.cursor()

                for statement in _CREATE_TABLES:
                    cursor.execute(statement)

                cursor.execute("PRAGMA table_info(recovery_cache)")
                existing_columns = {row[1] for row in cursor.fetchall()}
                for column, definition in _ADDED_CACHE_COLUMNS.items():
                    if column not in existing_columns:
                        cursor.execute(f"ALTER TABLE recovery_cache ADD COLUMN {column} {definition}")

                cursor.execute("""
                    UPDATE recovery_cache
                    SET last_accessed_at = cached_at
                    WHERE last_accessed_at IS NULL
                """)

                for name, target in _INDEXES.items():
                    cursor.execute(f"CREATE INDEX IF NOT EXISTS {name} ON {target}")

                conn.commit()
                self._logger.debug("Recovery cache tables initialized")

        except sqlite3.Error as e:
            self._logger.error(f"Failed to initialize recovery cache tables: {e}")

    def _url_hash(self, url: str) -> str:
        """Generate consistent hash for URL."""
        return hashlib.sha256(url.encode('utf-8')).hexdigest()[:16]

    def get_cached_result(self, url: str, source: RecoverySource) -> RecoveryCacheEntry | None:
        """Retrieve cached recovery result if available and not expired."""
        try:
            url_hash = self._url_hash(url)
            current_time = time.time()

            with self.sqlite_manager.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT id, url_hash, original_url, recovery_source, recovered_url,
                           content_quality, cached_at, last_accessed_at, expires_at, metadata_json, success
                    FROM recovery_cache
                    WHERE url_hash = ? AND recovery_source = ? AND expires_at > ?
                """, (url_hash, source.value, current_time))

                row = cursor.fetchone()
                if row:
                    # Update last_accessed_at for LRU tracking
                    cursor.execute("""
                        UPDATE recovery_cache
                        SET last_accessed_at = ?
                        WHERE id = ?
                    """, (current_time, row[0]))
                    conn.commit()

                    return RecoveryCacheEntry(
                        id=row[0],
                        url_hash=row[1],
                        original_url=row[2],
                        recovery_source=source,
                        recovered_url=row[4],
                        content_quality=RecoveryQuality(row[5]),
                        cached_at=row[6],
                        expires_at=row[8],  # Updated index after adding last_accessed_at
                        metadata_json=row[9],
                        success=bool(row[10]),
                    )

        except sqlite3.Error as e:
            self._logger.error(f"Failed to retrieve cached result: {e}")

        return None

    def cache_result(self, url: str, source: RecoverySource, recovered_url: str | None,
                    quality: RecoveryQuality, ttl_hours: int = 24,
                    metadata: dict[str, Any] | None = None,
                    success: bool = True) -> bool:
        """Cache a recovery result."""
        try:
            url_hash = self._url_hash(url)
            current_time = time.time()
            expires_at = current_time + timedelta(hours=ttl_hours).total_seconds()
            metadata_json = json.dumps(metadata) if metadata else None

            with self.sqlite_manager.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    INSERT OR REPLACE INTO recovery_cache
                    (url_hash, original_url, recovery_source, recovered_url,
                     content_quality, cached_at, last_accessed_at, expires_at, metadata_json, success)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    url_hash,
                    url,
                    source.value,
                    recovered_url,
                    quality.value,
                    current_time,
                    current_time,  # last_accessed_at starts as cached_at
                    expires_at,
                    metadata_json,
                    1 if success else 0,
                ))
                conn.commit()

            self._logger.debug(f"Cached recovery result for {url} from {source.value}")
            return True

        except sqlite3.Error as e:
            self._logger.error(f"Failed to cache recovery result: {e}")
            return False

    def record_attempt(self, attempt: RecoveryAttempt) -> bool:
        """Record a recovery attempt for analytics."""
        try:
            with self.sqlite_manager.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    INSERT INTO recovery_attempts
                    (original_url, recovery_source, attempted_at, success,
                     recovered_url, error_message, duration_seconds)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (
                    attempt.original_url,
                    attempt.recovery_source.value,
                    attempt.attempted_at,
                    1 if attempt.success else 0,
                    attempt.recovered_url,
                    attempt.error_message,
                    attempt.duration_seconds
                ))
                conn.commit()

            self._logger.debug(f"Recorded recovery attempt for {attempt.original_url}")
            return True

        except sqlite3.Error as e:
            self._logger.error(f"Failed to record recovery attempt: {e}")
            return False

    def cleanup_expired_cache(self) -> int:
        """Remove expired cache entries and return count of removed entries."""
        try:
            current_time = time.time()

            with self.sqlite_manager.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("DELETE FROM recovery_cache WHERE expires_at <= ?", (current_time,))
                removed_count = cursor.rowcount
                conn.commit()

            if removed_count > 0:
                self._logger.info(f"Cleaned up {removed_count} expired cache entries")

            return removed_count

        except sqlite3.Error as e:
            self._logger.error(f"Failed to cleanup expired cache: {e}")
            return 0

    @staticmethod
    def _evict_oldest(cursor: sqlite3.Cursor, count: int) -> int:
        """Delete the least recently used entries and return how many went."""
        cursor.execute("""
            DELETE FROM recovery_cache
            WHERE id IN (
                SELECT id FROM recovery_cache
                ORDER BY last_accessed_at ASC
                LIMIT ?
            )
        """, (count,))
        return cursor.rowcount

    def cleanup_lru_cache(self) -> int:
        """Remove least recently used cache entries when cache is over size limits."""
        try:
            with self.sqlite_manager.get_connection() as conn:
                cursor = conn.cursor()

                cursor.execute("SELECT COUNT(*) FROM recovery_cache")
                current_count = cursor.fetchone()[0]

                removed_count = 0

                if current_count > self.config.max_cache_entries:
                    removed_count = self._evict_oldest(cursor, current_count - self.config.max_cache_entries)
                    current_count -= removed_count
                    self._logger.info(f"Removed {removed_count} LRU cache entries (count limit: {self.config.max_cache_entries})")

                # Rough size estimate. Negative entries have NULL columns, which must still count.
                cursor.execute("""
                    SELECT SUM(COALESCE(LENGTH(metadata_json), 0)
                               + COALESCE(LENGTH(original_url), 0)
                               + COALESCE(LENGTH(recovered_url), 0)) / 1048576.0
                    FROM recovery_cache
                """)
                cache_size_mb = cursor.fetchone()[0] or 0

                if cache_size_mb > self.config.max_cache_size_mb:
                    target_reduction = int((cache_size_mb - self.config.max_cache_size_mb) / cache_size_mb * current_count)
                    size_removed_count = self._evict_oldest(cursor, max(target_reduction, 1))
                    removed_count += size_removed_count
                    self._logger.info(f"Removed {size_removed_count} LRU cache entries (size limit: {self.config.max_cache_size_mb}MB)")

                conn.commit()
                return removed_count

        except sqlite3.Error as e:
            self._logger.error(f"Failed to cleanup LRU cache: {e}")
            return 0

    def cleanup_cache(self) -> dict[str, int]:
        """Remove expired entries, then the least recently used ones over the size limit."""
        ttl_removed = self.cleanup_expired_cache()
        lru_removed = self.cleanup_lru_cache()

        total_removed = ttl_removed + lru_removed
        if total_removed > 0:
            self._logger.info(f"Cache cleanup completed: {ttl_removed} expired, {lru_removed} LRU, {total_removed} total")

        return {
            'ttl_removed': ttl_removed,
            'lru_removed': lru_removed,
            'total_removed': total_removed
        }

    def cleanup_if_due(self) -> None:
        """Clean up on a run's first lookup, then at most once per configured interval."""
        if not self.config.enable_background_cleanup:
            return
        with self._lock:
            now = time.monotonic()
            interval_seconds = self.config.cleanup_interval_minutes * 60
            if self._last_cleanup is not None and now - self._last_cleanup < interval_seconds:
                return
            self._last_cleanup = now
        self.cleanup_cache()
