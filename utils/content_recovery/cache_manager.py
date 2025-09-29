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
import threading
import configparser
import os
from typing import Optional, Dict, Any
from dataclasses import dataclass

from .recovery_metadata import RecoveryCacheEntry, RecoveryAttempt, RecoverySource, RecoveryQuality
from ..sqlite_manager import get_cache_manager


@dataclass
class CacheConfig:
    """Configuration for cache behavior."""
    cache_duration_hours: int = 24
    max_cache_entries: int = 10000
    max_cache_size_mb: int = 100
    cleanup_interval_minutes: int = 60
    enable_background_cleanup: bool = True


def load_cache_config() -> CacheConfig:
    """Load cache configuration from settings.ini with fallbacks to defaults."""
    config = configparser.ConfigParser()

    # Find settings.ini in the project root
    current_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.dirname(os.path.dirname(current_dir))
    config_path = os.path.join(project_root, 'settings.ini')

    # Use defaults if config file doesn't exist or section is missing
    cache_config = CacheConfig()

    try:
        if os.path.exists(config_path):
            config.read(config_path)

            if config.has_section('Recovery'):
                recovery_section = config['Recovery']
                cache_config.cache_duration_hours = recovery_section.getint('cache_duration_hours', cache_config.cache_duration_hours)
                cache_config.max_cache_entries = recovery_section.getint('max_cache_entries', cache_config.max_cache_entries)
                cache_config.max_cache_size_mb = recovery_section.getint('max_cache_size_mb', cache_config.max_cache_size_mb)
                cache_config.cleanup_interval_minutes = recovery_section.getint('cleanup_interval_minutes', cache_config.cleanup_interval_minutes)
                cache_config.enable_background_cleanup = recovery_section.getboolean('enable_background_cleanup', cache_config.enable_background_cleanup)

    except Exception as e:
        # Log warning but continue with defaults
        logging.getLogger(__name__).warning(f"Failed to load cache configuration: {e}. Using defaults.")

    return cache_config


class RecoveryCacheManager:
    """Manages SQLite-based caching for content recovery results."""

    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or "retry_queue.db"
        self._logger = logging.getLogger(f"{__name__}.{self.__class__.__name__}")
        self.config = load_cache_config()
        self._lock = threading.RLock()
        self._background_cleanup_timer = None
        self.sqlite_manager = get_cache_manager(self.db_path)
        self._ensure_tables()
        if self.config.enable_background_cleanup:
            self._start_background_cleanup()

    def _ensure_tables(self):
        """Create recovery tables if they don't exist."""
        try:
            with self.sqlite_manager.get_connection() as conn:
                cursor = conn.cursor()

                # Note: WAL mode and other optimizations are handled by sqlite_manager

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
                        last_accessed_at REAL NOT NULL,
                        expires_at REAL NOT NULL,
                        metadata_json TEXT,
                        UNIQUE(url_hash, recovery_source)
                    )
                """)

                # Add last_accessed_at column if it doesn't exist (migration)
                try:
                    cursor.execute("ALTER TABLE recovery_cache ADD COLUMN last_accessed_at REAL")
                except sqlite3.OperationalError:
                    # Column already exists or other error - continue
                    pass

                # Update existing records to have last_accessed_at = cached_at if NULL
                cursor.execute("""
                    UPDATE recovery_cache
                    SET last_accessed_at = cached_at
                    WHERE last_accessed_at IS NULL
                """)

                # Indexes for performance optimization (2024 best practices)

                # Primary lookup index - covers main cache query pattern
                cursor.execute("""
                    CREATE INDEX IF NOT EXISTS idx_recovery_cache_lookup
                    ON recovery_cache(url_hash, recovery_source, expires_at)
                """)

                # Individual column indexes for specific queries
                cursor.execute("""
                    CREATE INDEX IF NOT EXISTS idx_recovery_cache_url_hash
                    ON recovery_cache(url_hash)
                """)

                cursor.execute("""
                    CREATE INDEX IF NOT EXISTS idx_recovery_cache_expires
                    ON recovery_cache(expires_at)
                """)

                cursor.execute("""
                    CREATE INDEX IF NOT EXISTS idx_recovery_cache_source
                    ON recovery_cache(recovery_source)
                """)

                cursor.execute("""
                    CREATE INDEX IF NOT EXISTS idx_recovery_cache_cached_at
                    ON recovery_cache(cached_at)
                """)

                cursor.execute("""
                    CREATE INDEX IF NOT EXISTS idx_recovery_cache_last_accessed
                    ON recovery_cache(last_accessed_at)
                """)

                # Recovery attempts indexes
                cursor.execute("""
                    CREATE INDEX IF NOT EXISTS idx_recovery_attempts_url
                    ON recovery_attempts(original_url)
                """)

                cursor.execute("""
                    CREATE INDEX IF NOT EXISTS idx_recovery_attempts_source
                    ON recovery_attempts(recovery_source)
                """)

                cursor.execute("""
                    CREATE INDEX IF NOT EXISTS idx_recovery_attempts_time
                    ON recovery_attempts(attempted_at)
                """)

                # Composite index for statistics queries
                cursor.execute("""
                    CREATE INDEX IF NOT EXISTS idx_recovery_attempts_stats
                    ON recovery_attempts(recovery_source, attempted_at, success)
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

            with self.sqlite_manager.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT id, url_hash, original_url, recovery_source, recovered_url,
                           content_quality, cached_at, last_accessed_at, expires_at, metadata_json
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
                        recovery_source=row[3],
                        recovered_url=row[4],
                        content_quality=row[5],
                        cached_at=row[6],
                        expires_at=row[8],  # Updated index after adding last_accessed_at
                        metadata_json=row[9]  # Updated index
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

            with self.sqlite_manager.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    INSERT OR REPLACE INTO recovery_cache
                    (url_hash, original_url, recovery_source, recovered_url,
                     content_quality, cached_at, last_accessed_at, expires_at, metadata_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    cache_entry.url_hash,
                    cache_entry.original_url,
                    cache_entry.recovery_source,
                    cache_entry.recovered_url,
                    cache_entry.content_quality,
                    cache_entry.cached_at,
                    cache_entry.cached_at,  # Set last_accessed_at to cached_at initially
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
            with self.sqlite_manager.get_connection() as conn:
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

    def cleanup_lru_cache(self) -> int:
        """Remove least recently used cache entries when cache is over size limits."""
        try:
            with self.sqlite_manager.get_connection() as conn:
                cursor = conn.cursor()

                # Count current cache entries
                cursor.execute("SELECT COUNT(*) FROM recovery_cache")
                current_count = cursor.fetchone()[0]

                removed_count = 0

                # Remove excess entries by count
                if current_count > self.config.max_cache_entries:
                    excess_count = current_count - self.config.max_cache_entries
                    cursor.execute("""
                        DELETE FROM recovery_cache
                        WHERE id IN (
                            SELECT id FROM recovery_cache
                            ORDER BY last_accessed_at ASC
                            LIMIT ?
                        )
                    """, (excess_count,))
                    removed_count = cursor.rowcount
                    self._logger.info(f"Removed {removed_count} LRU cache entries (count limit: {self.config.max_cache_entries})")

                # Check cache size in MB (rough estimate based on metadata size)
                cursor.execute("""
                    SELECT SUM(LENGTH(metadata_json) + LENGTH(original_url) + LENGTH(recovered_url)) / 1048576.0
                    FROM recovery_cache
                """)
                size_result = cursor.fetchone()[0]
                cache_size_mb = size_result if size_result else 0

                if cache_size_mb > self.config.max_cache_size_mb:
                    # Remove oldest entries until under size limit
                    target_reduction = int((cache_size_mb - self.config.max_cache_size_mb) / cache_size_mb * current_count)
                    target_reduction = max(target_reduction, 1)  # Remove at least 1

                    cursor.execute("""
                        DELETE FROM recovery_cache
                        WHERE id IN (
                            SELECT id FROM recovery_cache
                            ORDER BY last_accessed_at ASC
                            LIMIT ?
                        )
                    """, (target_reduction,))
                    size_removed_count = cursor.rowcount
                    removed_count += size_removed_count
                    self._logger.info(f"Removed {size_removed_count} LRU cache entries (size limit: {self.config.max_cache_size_mb}MB)")

                conn.commit()
                return removed_count

        except sqlite3.Error as e:
            self._logger.error(f"Failed to cleanup LRU cache: {e}")
            return 0

    def cleanup_cache(self) -> Dict[str, int]:
        """Perform comprehensive cache cleanup (both TTL and LRU)."""
        with self._lock:
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

    def _start_background_cleanup(self):
        """Start background cleanup timer."""
        if self._background_cleanup_timer is not None:
            return  # Already started

        def cleanup_task():
            try:
                self.cleanup_cache()
            except Exception as e:
                self._logger.error(f"Background cache cleanup failed: {e}")
            finally:
                # Schedule next cleanup
                if self.config.enable_background_cleanup:
                    interval_seconds = self.config.cleanup_interval_minutes * 60
                    self._background_cleanup_timer = threading.Timer(interval_seconds, cleanup_task)
                    self._background_cleanup_timer.daemon = True
                    self._background_cleanup_timer.start()

        # Start first cleanup
        interval_seconds = self.config.cleanup_interval_minutes * 60
        self._background_cleanup_timer = threading.Timer(interval_seconds, cleanup_task)
        self._background_cleanup_timer.daemon = True
        self._background_cleanup_timer.start()
        self._logger.info(f"Background cache cleanup started (interval: {self.config.cleanup_interval_minutes} minutes)")

    def stop_background_cleanup(self):
        """Stop background cleanup timer."""
        if self._background_cleanup_timer is not None:
            self._background_cleanup_timer.cancel()
            self._background_cleanup_timer = None
            self._logger.info("Background cache cleanup stopped")

    def __del__(self):
        """Cleanup when object is destroyed."""
        try:
            self.stop_background_cleanup()
            self.optimize_database()
        except Exception:
            # Ignore errors during cleanup
            pass

    def get_recovery_statistics(self, days: int = 30) -> Dict[str, Any]:
        """Get recovery statistics for the specified number of days."""
        try:
            cutoff_time = time.time() - (days * 24 * 3600)

            with self.sqlite_manager.get_connection() as conn:
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
            with self.sqlite_manager.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT COUNT(*) FROM recovery_cache")
                return cursor.fetchone()[0]
        except sqlite3.Error as e:
            self._logger.error(f"Failed to get cache size: {e}")
            return 0

    def optimize_database(self) -> bool:
        """
        Optimize database performance using PRAGMA optimize.

        Should be called periodically or before closing connections.
        Based on 2024 SQLite performance best practices.

        Returns:
            True if optimization succeeded, False otherwise
        """
        try:
            with self.sqlite_manager.get_connection() as conn:
                cursor = conn.cursor()

                # Run PRAGMA optimize to update statistics and improve query performance
                cursor.execute("PRAGMA optimize")

                # Update table statistics
                cursor.execute("ANALYZE")

                conn.commit()
                self._logger.debug("Database optimization completed")
                return True

        except sqlite3.Error as e:
            self._logger.error(f"Failed to optimize database: {e}")
            return False

    def vacuum_database(self, auto_vacuum: bool = True) -> bool:
        """
        Vacuum the database to reclaim space and improve performance.

        Args:
            auto_vacuum: Enable auto-vacuum mode for future operations

        Returns:
            True if vacuum succeeded, False otherwise
        """
        try:
            with self.sqlite_manager.get_connection() as conn:
                cursor = conn.cursor()

                # Enable auto-vacuum if requested
                if auto_vacuum:
                    cursor.execute("PRAGMA auto_vacuum = FULL")

                # Vacuum the database to reclaim space
                cursor.execute("VACUUM")

                self._logger.info("Database vacuum completed")
                return True

        except sqlite3.Error as e:
            self._logger.error(f"Failed to vacuum database: {e}")
            return False

    def get_database_info(self) -> Dict[str, Any]:
        """
        Get detailed database information for monitoring.

        Returns:
            Dictionary with database statistics and performance metrics
        """
        try:
            with self.sqlite_manager.get_connection() as conn:
                cursor = conn.cursor()

                info = {}

                # Database file size
                cursor.execute("PRAGMA page_size")
                page_size = cursor.fetchone()[0]

                cursor.execute("PRAGMA page_count")
                page_count = cursor.fetchone()[0]

                info['file_size_bytes'] = page_size * page_count
                info['page_size'] = page_size
                info['page_count'] = page_count

                # Journal mode and cache settings
                cursor.execute("PRAGMA journal_mode")
                info['journal_mode'] = cursor.fetchone()[0]

                cursor.execute("PRAGMA cache_size")
                info['cache_size'] = cursor.fetchone()[0]

                # Table counts
                cursor.execute("SELECT COUNT(*) FROM recovery_cache")
                info['cache_entries'] = cursor.fetchone()[0]

                cursor.execute("SELECT COUNT(*) FROM recovery_attempts")
                info['attempt_entries'] = cursor.fetchone()[0]

                # Index information
                cursor.execute("""
                    SELECT name FROM sqlite_master
                    WHERE type='index' AND sql IS NOT NULL
                """)
                info['indexes'] = [row[0] for row in cursor.fetchall()]

                return info

        except sqlite3.Error as e:
            self._logger.error(f"Failed to get database info: {e}")
            return {}

