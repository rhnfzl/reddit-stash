"""
SQLite-based cache manager for content recovery system.

This module handles caching of recovery results to avoid repeated API calls
and improve performance. Uses the same SQLite database as the retry queue
for consistency.
"""

import json
import time
import hashlib
import sqlite3
import logging
from typing import Optional, List, Dict, Any
from pathlib import Path

from .recovery_metadata import RecoveryCacheEntry, RecoveryAttempt, RecoverySource, RecoveryQuality


class RecoveryCacheManager:
    """Manages SQLite-based caching for content recovery results."""

    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or "retry_queue.db"
        self._logger = logging.getLogger(f"{__name__}.{self.__class__.__name__}")
        self._ensure_tables()

    def _ensure_tables(self):
        """Create recovery tables if they don't exist."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()

                # Recovery attempts table
                cursor.execute("""
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
                """)

                # Recovery cache table
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS recovery_cache (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        url_hash TEXT NOT NULL,
                        original_url TEXT NOT NULL,
                        recovery_source TEXT NOT NULL,
                        recovered_url TEXT,
                        content_quality TEXT DEFAULT 'medium_quality',
                        cached_at REAL NOT NULL,
                        expires_at REAL NOT NULL,
                        metadata_json TEXT,
                        UNIQUE(url_hash, recovery_source)
                    )
                """)

                # Indexes for performance
                cursor.execute("""
                    CREATE INDEX IF NOT EXISTS idx_recovery_cache_url_hash
                    ON recovery_cache(url_hash)
                """)

                cursor.execute("""
                    CREATE INDEX IF NOT EXISTS idx_recovery_cache_expires
                    ON recovery_cache(expires_at)
                """)

                cursor.execute("""
                    CREATE INDEX IF NOT EXISTS idx_recovery_attempts_url
                    ON recovery_attempts(original_url)
                """)

                conn.commit()
                self._logger.debug("Recovery cache tables initialized")

        except sqlite3.Error as e:
            self._logger.error(f"Failed to initialize recovery cache tables: {e}")

    def _url_hash(self, url: str) -> str:
        """Generate consistent hash for URL."""
        return hashlib.sha256(url.encode('utf-8')).hexdigest()[:16]

    def get_cached_result(self, url: str, source: RecoverySource) -> Optional[RecoveryCacheEntry]:
        """Retrieve cached recovery result if available and not expired."""
        try:
            url_hash = self._url_hash(url)
            current_time = time.time()

            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT id, url_hash, original_url, recovery_source, recovered_url,
                           content_quality, cached_at, expires_at, metadata_json
                    FROM recovery_cache
                    WHERE url_hash = ? AND recovery_source = ? AND expires_at > ?
                """, (url_hash, source.value, current_time))

                row = cursor.fetchone()
                if row:
                    return RecoveryCacheEntry(
                        id=row[0],
                        url_hash=row[1],
                        original_url=row[2],
                        recovery_source=row[3],
                        recovered_url=row[4],
                        content_quality=row[5],
                        cached_at=row[6],
                        expires_at=row[7],
                        metadata_json=row[8]
                    )

        except sqlite3.Error as e:
            self._logger.error(f"Failed to retrieve cached result: {e}")

        return None

    def cache_result(self, url: str, source: RecoverySource, recovered_url: Optional[str],
                    quality: RecoveryQuality, ttl_hours: int = 24,
                    metadata: Optional[Dict[str, Any]] = None) -> bool:
        """Cache a recovery result."""
        try:
            url_hash = self._url_hash(url)
            current_time = time.time()
            expires_at = current_time + (ttl_hours * 3600)
            metadata_json = json.dumps(metadata) if metadata else None

            cache_entry = RecoveryCacheEntry(
                url_hash=url_hash,
                original_url=url,
                recovery_source=source.value,
                recovered_url=recovered_url,
                content_quality=quality.value,
                cached_at=current_time,
                expires_at=expires_at,
                metadata_json=metadata_json
            )

            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    INSERT OR REPLACE INTO recovery_cache
                    (url_hash, original_url, recovery_source, recovered_url,
                     content_quality, cached_at, expires_at, metadata_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    cache_entry.url_hash,
                    cache_entry.original_url,
                    cache_entry.recovery_source,
                    cache_entry.recovered_url,
                    cache_entry.content_quality,
                    cache_entry.cached_at,
                    cache_entry.expires_at,
                    cache_entry.metadata_json
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
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    INSERT INTO recovery_attempts
                    (original_url, recovery_source, attempted_at, success,
                     recovered_url, error_message, duration_seconds)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (
                    attempt.original_url,
                    attempt.recovery_source,
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

            with sqlite3.connect(self.db_path) as conn:
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

    def get_recovery_statistics(self, days: int = 30) -> Dict[str, Any]:
        """Get recovery statistics for the specified number of days."""
        try:
            cutoff_time = time.time() - (days * 24 * 3600)

            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()

                # Overall statistics
                cursor.execute("""
                    SELECT recovery_source, COUNT(*), SUM(success), AVG(duration_seconds)
                    FROM recovery_attempts
                    WHERE attempted_at > ?
                    GROUP BY recovery_source
                """, (cutoff_time,))

                stats = {}
                for row in cursor.fetchall():
                    source, total, successes, avg_duration = row
                    stats[source] = {
                        'total_attempts': total,
                        'successful': successes or 0,
                        'success_rate': (successes or 0) / total if total > 0 else 0,
                        'average_duration': avg_duration or 0
                    }

                return stats

        except sqlite3.Error as e:
            self._logger.error(f"Failed to get recovery statistics: {e}")
            return {}

    def get_cache_size(self) -> int:
        """Get the current size of the recovery cache."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT COUNT(*) FROM recovery_cache")
                return cursor.fetchone()[0]
        except sqlite3.Error as e:
            self._logger.error(f"Failed to get cache size: {e}")
            return 0