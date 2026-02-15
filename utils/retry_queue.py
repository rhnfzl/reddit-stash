"""
Retry queue system with persistent storage and intelligent retry logic.

This module implements a sophisticated retry queue system following 2024 best practices:
- Persistent storage using SQLite for reliability across application restarts
- Exponential backoff with jitter for retry timing
- Priority-based retry ordering
- Dead letter queue for permanently failed items
- Circuit breaker integration for unhealthy services
- Metrics and monitoring support
"""

import sqlite3
import json
import time
import threading
import logging
import configparser
import os
from typing import Dict, Any, Optional, List
from dataclasses import dataclass
from pathlib import Path
from enum import Enum
import random

from .sqlite_manager import get_retry_queue_manager


@dataclass
class RetryConfig:
    """Configuration for retry queue behavior."""
    max_retries: int = 5
    base_retry_delay_high: int = 5
    base_retry_delay_medium: int = 10
    base_retry_delay_low: int = 15
    exponential_base_delay: int = 60
    max_retry_delay: int = 86400  # 24 hours
    dead_letter_threshold_days: int = 7


def load_retry_config() -> RetryConfig:
    """Load retry configuration from settings.ini with fallbacks to defaults."""
    config = configparser.ConfigParser()

    # Find settings.ini in the project root
    current_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.dirname(current_dir)
    config_path = os.path.join(project_root, 'settings.ini')

    # Use defaults if config file doesn't exist or section is missing
    retry_config = RetryConfig()

    try:
        if os.path.exists(config_path):
            config.read(config_path)

            if config.has_section('Retry'):
                retry_section = config['Retry']
                retry_config.max_retries = retry_section.getint('max_retries', retry_config.max_retries)
                retry_config.base_retry_delay_high = retry_section.getint('base_retry_delay_high', retry_config.base_retry_delay_high)
                retry_config.base_retry_delay_medium = retry_section.getint('base_retry_delay_medium', retry_config.base_retry_delay_medium)
                retry_config.base_retry_delay_low = retry_section.getint('base_retry_delay_low', retry_config.base_retry_delay_low)
                retry_config.exponential_base_delay = retry_section.getint('exponential_base_delay', retry_config.exponential_base_delay)
                retry_config.max_retry_delay = retry_section.getint('max_retry_delay', retry_config.max_retry_delay)
                retry_config.dead_letter_threshold_days = retry_section.getint('dead_letter_threshold_days', retry_config.dead_letter_threshold_days)

    except Exception as e:
        # Log warning but continue with defaults
        logging.getLogger(__name__).warning(f"Failed to load retry configuration: {e}. Using defaults.")

    return retry_config


class RetryStatus(Enum):
    """Status of retry items."""
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED_PERMANENT = "failed_permanent"
    DEAD_LETTER = "dead_letter"


@dataclass
class RetryItem:
    """A single item in the retry queue."""
    url: str
    service_name: str
    error_message: str
    retry_count: int = 0
    max_retries: int = 5
    priority: int = 1  # 1=high, 2=medium, 3=low
    created_at: float = 0.0
    next_retry_at: float = 0.0
    last_attempt_at: Optional[float] = None
    status: RetryStatus = RetryStatus.PENDING
    metadata: Dict[str, Any] = None
    config: Optional[RetryConfig] = None

    def __post_init__(self):
        if self.config is None:
            self.config = load_retry_config()
        if self.created_at == 0.0:
            self.created_at = time.time()
        if self.next_retry_at == 0.0:
            self.next_retry_at = self.created_at + self._calculate_initial_delay()
        if self.metadata is None:
            self.metadata = {}
        # Update max_retries from config if not explicitly set
        if self.max_retries == 5:  # Default value
            self.max_retries = self.config.max_retries

    def _calculate_initial_delay(self) -> float:
        """Calculate initial delay based on priority using configuration."""
        base_delays = {
            1: self.config.base_retry_delay_high,
            2: self.config.base_retry_delay_medium,
            3: self.config.base_retry_delay_low
        }
        return base_delays.get(self.priority, self.config.base_retry_delay_medium)

    def calculate_next_retry_delay(self) -> float:
        """Calculate delay until next retry using exponential backoff with jitter."""
        if self.retry_count == 0:
            base_delay = self._calculate_initial_delay()
        else:
            # Exponential backoff: 2^attempt * base_delay
            base_delay = (2 ** self.retry_count) * self.config.exponential_base_delay

        # Add jitter (±25%)
        jitter = random.uniform(0.75, 1.25)
        delay = base_delay * jitter

        # Cap maximum delay using configuration
        return min(delay, self.config.max_retry_delay)

    def is_ready_for_retry(self) -> bool:
        """Check if item is ready for retry."""
        return (
            self.status == RetryStatus.PENDING and
            self.retry_count < self.max_retries and
            time.time() >= self.next_retry_at
        )

    def should_move_to_dead_letter(self) -> bool:
        """Check if item should be moved to dead letter queue."""
        dead_letter_threshold_seconds = self.config.dead_letter_threshold_days * 24 * 3600
        return (
            self.retry_count >= self.max_retries or
            self.status == RetryStatus.FAILED_PERMANENT or
            (time.time() - self.created_at) > dead_letter_threshold_seconds
        )

    def increment_retry(self) -> None:
        """Increment retry count and update timing."""
        self.retry_count += 1
        self.last_attempt_at = time.time()

        if self.retry_count >= self.max_retries:
            self.status = RetryStatus.FAILED_PERMANENT
        else:
            delay = self.calculate_next_retry_delay()
            self.next_retry_at = time.time() + delay
            self.status = RetryStatus.PENDING


class SQLiteRetryQueue:
    """
    SQLite-based persistent retry queue with intelligent retry logic.

    Implements RetryQueueProtocol for dependency injection compatibility.
    Provides ACID transactions, efficient indexing, and automatic cleanup.
    """

    def __init__(self, database_path: str = "reddit_stash_retry_queue.db"):
        self.database_path = Path(database_path)
        self._lock = threading.RLock()
        self._logger = logging.getLogger(f"{__name__}.{self.__class__.__name__}")
        self.config = load_retry_config()
        self.sqlite_manager = get_retry_queue_manager(str(database_path))
        self._initialize_database()

    def _safe_json_loads(self, raw) -> dict:
        """Safely parse JSON metadata, returning empty dict on failure."""
        try:
            return json.loads(raw) if raw else {}
        except (json.JSONDecodeError, TypeError):
            self._logger.debug("Invalid JSON metadata, using empty dict")
            return {}

    def _initialize_database(self) -> None:
        """Initialize SQLite database with proper schema and indexes."""
        with self.sqlite_manager.get_connection() as conn:
            # Note: WAL mode and other optimizations are handled by sqlite_manager

            # Create main retry queue table
            conn.execute(f"""
                CREATE TABLE IF NOT EXISTS retry_queue (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    url TEXT NOT NULL,
                    service_name TEXT NOT NULL,
                    error_message TEXT NOT NULL,
                    retry_count INTEGER DEFAULT 0,
                    max_retries INTEGER DEFAULT {self.config.max_retries},
                    priority INTEGER DEFAULT 1,
                    created_at REAL NOT NULL,
                    next_retry_at REAL NOT NULL,
                    last_attempt_at REAL,
                    status TEXT DEFAULT 'pending',
                    metadata TEXT DEFAULT '{{}}',
                    UNIQUE(url, service_name)
                )
            """)

            # Create dead letter queue table
            conn.execute("""
                CREATE TABLE IF NOT EXISTS dead_letter_queue (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    url TEXT NOT NULL,
                    service_name TEXT NOT NULL,
                    error_message TEXT NOT NULL,
                    retry_count INTEGER,
                    created_at REAL NOT NULL,
                    moved_to_dlq_at REAL NOT NULL,
                    metadata TEXT DEFAULT '{}'
                )
            """)

            # Create indexes for efficient queries
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_retry_queue_status_priority
                ON retry_queue(status, priority, next_retry_at)
            """)

            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_retry_queue_service
                ON retry_queue(service_name, status)
            """)

            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_retry_queue_ready
                ON retry_queue(status, next_retry_at)
                WHERE status = 'pending'
            """)

            conn.commit()
            self._logger.info(f"Initialized retry queue database at {self.database_path}")

    def add_failed_download(self, url: str, error: str, service_name: str,
                          priority: int = 1, max_retries: Optional[int] = None,
                          metadata: Optional[Dict[str, Any]] = None) -> None:
        """Add a failed download to the retry queue."""
        # Use config default if max_retries not specified
        if max_retries is None:
            max_retries = self.config.max_retries

        retry_item = RetryItem(
            url=url,
            service_name=service_name,
            error_message=error,
            priority=priority,
            max_retries=max_retries,
            metadata=metadata or {},
            config=self.config
        )

        with self._lock:
            try:
                with self.sqlite_manager.get_connection() as conn:
                    # Use INSERT OR REPLACE to handle duplicates
                    conn.execute("""
                        INSERT OR REPLACE INTO retry_queue
                        (url, service_name, error_message, retry_count, max_retries,
                         priority, created_at, next_retry_at, status, metadata)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        retry_item.url,
                        retry_item.service_name,
                        retry_item.error_message,
                        retry_item.retry_count,
                        retry_item.max_retries,
                        retry_item.priority,
                        retry_item.created_at,
                        retry_item.next_retry_at,
                        retry_item.status.value,
                        json.dumps(retry_item.metadata)
                    ))

                    conn.commit()
                    self._logger.info(f"Added failed download to retry queue: {url} (service: {service_name})")

            except sqlite3.Error as e:
                self._logger.error(f"Failed to add item to retry queue: {e}")

    def get_pending_retries(self, service_name: Optional[str] = None,
                          limit: int = 50) -> List[Dict[str, Any]]:
        """Get pending retry items, optionally filtered by service."""
        with self._lock:
            try:
                with self.sqlite_manager.get_connection() as conn:
                    conn.row_factory = sqlite3.Row

                    # Build query based on filters
                    if service_name:
                        query = """
                            SELECT * FROM retry_queue
                            WHERE status = 'pending' AND service_name = ? AND next_retry_at <= ?
                            ORDER BY priority ASC, next_retry_at ASC
                            LIMIT ?
                        """
                        params = (service_name, time.time(), limit)
                    else:
                        query = """
                            SELECT * FROM retry_queue
                            WHERE status = 'pending' AND next_retry_at <= ?
                            ORDER BY priority ASC, next_retry_at ASC
                            LIMIT ?
                        """
                        params = (time.time(), limit)

                    cursor = conn.execute(query, params)
                    rows = cursor.fetchall()

                    # Convert to list of dictionaries
                    items = []
                    for row in rows:
                        item_dict = dict(row)
                        item_dict['metadata'] = self._safe_json_loads(item_dict['metadata'])
                        items.append(item_dict)

                    return items

            except sqlite3.Error as e:
                self._logger.error(f"Failed to get pending retries: {e}")
                return []

    def mark_retry_started(self, url: str, service_name: str) -> bool:
        """Mark a retry as started (in progress)."""
        with self._lock:
            try:
                with self.sqlite_manager.get_connection() as conn:
                    cursor = conn.execute("""
                        UPDATE retry_queue
                        SET status = 'in_progress', last_attempt_at = ?
                        WHERE url = ? AND service_name = ? AND status = 'pending'
                    """, (time.time(), url, service_name))

                    conn.commit()
                    return cursor.rowcount > 0

            except sqlite3.Error as e:
                self._logger.error(f"Failed to mark retry as started: {e}")
                return False

    def mark_retry_completed(self, url: str, success: bool,
                           error_message: Optional[str] = None) -> None:
        """Mark a retry as completed."""
        with self._lock:
            try:
                with self.sqlite_manager.get_connection() as conn:
                    if success:
                        # Remove from retry queue on success
                        conn.execute("""
                            DELETE FROM retry_queue
                            WHERE url = ? AND status = 'in_progress'
                        """, (url,))
                        self._logger.info(f"Successful retry removed from queue: {url}")

                    else:
                        # Increment retry count and reschedule or move to dead letter
                        cursor = conn.execute("""
                            SELECT * FROM retry_queue
                            WHERE url = ? AND status = 'in_progress'
                        """, (url,))

                        row = cursor.fetchone()
                        if row:
                            retry_item = RetryItem(
                                url=row[1],
                                service_name=row[2],
                                error_message=error_message or row[3],
                                retry_count=row[4],
                                max_retries=row[5],
                                priority=row[6],
                                created_at=row[7],
                                next_retry_at=row[8],
                                last_attempt_at=row[9],
                                status=RetryStatus(row[10]),
                                metadata=self._safe_json_loads(row[11])
                            )

                            retry_item.increment_retry()

                            if retry_item.should_move_to_dead_letter():
                                self._move_to_dead_letter_queue(conn, retry_item)
                            else:
                                # Update for next retry
                                conn.execute("""
                                    UPDATE retry_queue
                                    SET retry_count = ?, next_retry_at = ?, status = ?,
                                        error_message = ?, last_attempt_at = ?
                                    WHERE url = ? AND status = 'in_progress'
                                """, (
                                    retry_item.retry_count,
                                    retry_item.next_retry_at,
                                    retry_item.status.value,
                                    retry_item.error_message,
                                    retry_item.last_attempt_at,
                                    url
                                ))

                    conn.commit()

            except sqlite3.Error as e:
                self._logger.error(f"Failed to mark retry as completed: {e}")

    def _move_to_dead_letter_queue(self, conn: sqlite3.Connection,
                                 retry_item: RetryItem) -> None:
        """Move an item to the dead letter queue."""
        try:
            # Insert into dead letter queue
            conn.execute("""
                INSERT INTO dead_letter_queue
                (url, service_name, error_message, retry_count, created_at,
                 moved_to_dlq_at, metadata)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                retry_item.url,
                retry_item.service_name,
                retry_item.error_message,
                retry_item.retry_count,
                retry_item.created_at,
                time.time(),
                json.dumps(retry_item.metadata)
            ))

            # Remove from retry queue
            conn.execute("""
                DELETE FROM retry_queue WHERE url = ? AND service_name = ?
            """, (retry_item.url, retry_item.service_name))

            self._logger.warning(f"Moved to dead letter queue: {retry_item.url} "
                               f"(service: {retry_item.service_name}, "
                               f"retries: {retry_item.retry_count})")

        except sqlite3.Error as e:
            self._logger.error(f"Failed to move item to dead letter queue: {e}")

    def cleanup_expired_retries(self, max_age_days: int = 30) -> int:
        """Remove expired retry items and return count removed."""
        cutoff_time = time.time() - (max_age_days * 24 * 3600)

        with self._lock:
            try:
                with self.sqlite_manager.get_connection() as conn:
                    # Move very old items to dead letter queue
                    cursor = conn.execute("""
                        SELECT * FROM retry_queue
                        WHERE created_at < ? AND status != 'in_progress'
                    """, (cutoff_time,))

                    old_items = cursor.fetchall()
                    removed_count = 0

                    for row in old_items:
                        retry_item = RetryItem(
                            url=row[1],
                            service_name=row[2],
                            error_message=f"Expired after {max_age_days} days: {row[3]}",
                            retry_count=row[4],
                            max_retries=row[5],
                            priority=row[6],
                            created_at=row[7],
                            metadata=self._safe_json_loads(row[11])
                        )

                        self._move_to_dead_letter_queue(conn, retry_item)
                        removed_count += 1

                    # Also cleanup very old dead letter items
                    dlq_cutoff = time.time() - (90 * 24 * 3600)  # 90 days
                    cursor = conn.execute("""
                        DELETE FROM dead_letter_queue WHERE moved_to_dlq_at < ?
                    """, (dlq_cutoff,))

                    dlq_removed = cursor.rowcount

                    conn.commit()

                    if removed_count > 0 or dlq_removed > 0:
                        self._logger.info(f"Cleanup completed: {removed_count} items moved to DLQ, "
                                        f"{dlq_removed} old DLQ items removed")

                    return removed_count

            except sqlite3.Error as e:
                self._logger.error(f"Failed to cleanup expired retries: {e}")
                return 0

    def get_queue_statistics(self) -> Dict[str, Any]:
        """Get statistics about the retry queue."""
        with self._lock:
            try:
                with self.sqlite_manager.get_connection() as conn:
                    stats = {}

                    # Retry queue stats
                    cursor = conn.execute("""
                        SELECT status, COUNT(*) as count FROM retry_queue GROUP BY status
                    """)

                    retry_stats = {row[0]: row[1] for row in cursor.fetchall()}
                    stats['retry_queue'] = retry_stats

                    # Service breakdown
                    cursor = conn.execute("""
                        SELECT service_name, COUNT(*) as count
                        FROM retry_queue
                        WHERE status = 'pending'
                        GROUP BY service_name
                    """)

                    service_stats = {row[0]: row[1] for row in cursor.fetchall()}
                    stats['pending_by_service'] = service_stats

                    # Dead letter queue stats
                    cursor = conn.execute("SELECT COUNT(*) FROM dead_letter_queue")
                    stats['dead_letter_count'] = cursor.fetchone()[0]

                    # Ready for retry count
                    cursor = conn.execute("""
                        SELECT COUNT(*) FROM retry_queue
                        WHERE status = 'pending' AND next_retry_at <= ?
                    """, (time.time(),))
                    stats['ready_for_retry'] = cursor.fetchone()[0]

                    return stats

            except sqlite3.Error as e:
                self._logger.error(f"Failed to get queue statistics: {e}")
                return {}

    def get_dead_letter_items(self, limit: int = 100) -> List[Dict[str, Any]]:
        """Get items from the dead letter queue."""
        with self._lock:
            try:
                with self.sqlite_manager.get_connection() as conn:
                    conn.row_factory = sqlite3.Row

                    cursor = conn.execute("""
                        SELECT * FROM dead_letter_queue
                        ORDER BY moved_to_dlq_at DESC
                        LIMIT ?
                    """, (limit,))

                    rows = cursor.fetchall()

                    items = []
                    for row in rows:
                        item_dict = dict(row)
                        item_dict['metadata'] = self._safe_json_loads(item_dict['metadata'])
                        items.append(item_dict)

                    return items

            except sqlite3.Error as e:
                self._logger.error(f"Failed to get dead letter items: {e}")
                return []

    def requeue_from_dead_letter(self, url: str, service_name: str) -> bool:
        """Move an item from dead letter queue back to retry queue."""
        with self._lock:
            try:
                with self.sqlite_manager.get_connection() as conn:
                    # Get item from dead letter queue
                    cursor = conn.execute("""
                        SELECT * FROM dead_letter_queue
                        WHERE url = ? AND service_name = ?
                    """, (url, service_name))

                    row = cursor.fetchone()
                    if not row:
                        return False

                    # Create new retry item with reset retry count
                    # Handle potential None metadata
                    metadata_str = row[6] if row[6] is not None else '{}'
                    retry_item = RetryItem(
                        url=row[1],
                        service_name=row[2],
                        error_message=f"Requeued from DLQ: {row[3]}",
                        retry_count=0,
                        max_retries=5,
                        priority=1,
                        metadata=json.loads(metadata_str) if isinstance(metadata_str, str) else {}
                    )

                    # Insert back into retry queue
                    conn.execute("""
                        INSERT OR REPLACE INTO retry_queue
                        (url, service_name, error_message, retry_count, max_retries,
                         priority, created_at, next_retry_at, status, metadata)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        retry_item.url,
                        retry_item.service_name,
                        retry_item.error_message,
                        retry_item.retry_count,
                        retry_item.max_retries,
                        retry_item.priority,
                        retry_item.created_at,
                        retry_item.next_retry_at,
                        retry_item.status.value,
                        json.dumps(retry_item.metadata)
                    ))

                    # Remove from dead letter queue
                    conn.execute("""
                        DELETE FROM dead_letter_queue
                        WHERE url = ? AND service_name = ?
                    """, (url, service_name))

                    conn.commit()
                    self._logger.info(f"Requeued from dead letter: {url}")
                    return True

            except sqlite3.Error as e:
                self._logger.error(f"Failed to requeue from dead letter: {e}")
                return False

    def close(self) -> None:
        """Close database connections and cleanup."""
        # SQLite connections are closed automatically in context managers
        # This method is provided for interface compliance
        pass


# Global retry queue instance
retry_queue = SQLiteRetryQueue()


def setup_retry_queue(database_path: Optional[str] = None) -> SQLiteRetryQueue:
    """Setup and return the global retry queue instance."""
    global retry_queue
    if database_path:
        retry_queue = SQLiteRetryQueue(database_path)
    return retry_queue


def get_retry_queue() -> SQLiteRetryQueue:
    """Get the global retry queue instance."""
    return retry_queue