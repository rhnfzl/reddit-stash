"""
SQLite-backed retry queue for failed downloads, kept across runs.

Items back off exponentially with jitter, and move to a dead letter queue when
their attempts run out.
"""

import sqlite3
import json
import time
import threading
import logging
from typing import Any
from dataclasses import dataclass, field
from enum import Enum
import random

from utils.constants import SECONDS_PER_DAY
from utils.env_config import load_settings
from utils.sqlite_manager import get_sqlite_manager


@dataclass
class RetryConfig:
    """Configuration for retry queue behavior."""
    max_retries: int = 5
    base_retry_delay_high: int = 5
    base_retry_delay_medium: int = 10
    base_retry_delay_low: int = 15
    exponential_base_delay: int = 60
    max_retry_delay: int = SECONDS_PER_DAY
    dead_letter_threshold_days: int = 7


def load_retry_config() -> RetryConfig:
    """Load retry configuration from settings.ini with fallbacks to defaults."""
    # Use defaults if config file doesn't exist or section is missing
    retry_config = RetryConfig()

    try:
        config = load_settings()

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
    FAILED_PERMANENT = "failed_permanent"


@dataclass
class RetryItem:
    """A single item in the retry queue."""
    url: str
    service_name: str
    error_message: str
    retry_count: int = 0
    max_retries: int | None = None  # None takes the configured value
    priority: int = 1  # 1=high, 2=medium, 3=low
    created_at: float = 0.0
    next_retry_at: float = 0.0
    last_attempt_at: float | None = None
    status: RetryStatus = RetryStatus.PENDING
    metadata: dict[str, Any] = field(default_factory=dict)
    config: RetryConfig = field(default_factory=load_retry_config)

    def __post_init__(self):
        if self.created_at == 0.0:
            self.created_at = time.time()
        if self.next_retry_at == 0.0:
            self.next_retry_at = self.created_at + self._calculate_initial_delay()
        if self.max_retries is None:
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

    def should_move_to_dead_letter(self) -> bool:
        """Check if item should be moved to dead letter queue."""
        dead_letter_threshold_seconds = self.config.dead_letter_threshold_days * SECONDS_PER_DAY
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
    SQLite-based persistent retry queue.

    Provides ACID transactions and efficient indexing.
    """

    def __init__(self, db_path: str = "reddit_stash_retry_queue.db"):
        self.db_path = db_path
        self._lock = threading.RLock()
        self._logger = logging.getLogger(f"{__name__}.{self.__class__.__name__}")
        self.config = load_retry_config()
        self.sqlite_manager = get_sqlite_manager(db_path)
        self._ensure_tables()

    def _safe_json_loads(self, raw) -> dict:
        """Safely parse JSON metadata, returning empty dict on failure."""
        try:
            metadata = json.loads(raw) if raw else {}
        except (json.JSONDecodeError, TypeError):
            self._logger.debug("Invalid JSON metadata, using empty dict")
            return {}
        return metadata if isinstance(metadata, dict) else {}

    def _ensure_tables(self) -> None:
        """Initialize SQLite database with proper schema and indexes."""
        with self.sqlite_manager.get_connection() as conn:
            # Note: WAL mode and other optimizations are handled by sqlite_manager

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
            self._logger.info(f"Initialized retry queue database at {self.db_path}")

    def add_failed_download(self, url: str, error: str, service_name: str,
                          priority: int = 1, max_retries: int | None = None,
                          metadata: dict[str, Any] | None = None) -> bool:
        """Add a failed download to the retry queue.

        Returns whether the URL is queued for a later run; False means the database
        failed and the failure is lost.
        """
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
                    # A row left in_progress by a crashed retry pass keeps its retry_count;
                    # replacing it here would reset the count to 0. The next pass requeues it.
                    if conn.execute(
                        "SELECT 1 FROM retry_queue WHERE url = ? AND service_name = ? AND status = 'in_progress'",
                        (url, service_name),
                    ).fetchone():
                        return True

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
                    self._logger.debug(f"Added failed download to retry queue: {url} (service: {service_name})")
                    return True

            except sqlite3.Error as e:
                self._logger.error(f"Failed to add item to retry queue: {e}")
                return False

    def reset_stale_in_progress(self) -> int:
        """Return rows stuck in_progress to pending so they are retried again."""
        with self._lock:
            try:
                with self.sqlite_manager.get_connection() as conn:
                    cursor = conn.execute(
                        "UPDATE retry_queue SET status = 'pending' WHERE status = 'in_progress'"
                    )
                    conn.commit()
                    return cursor.rowcount
            except sqlite3.Error as e:
                self._logger.error(f"Failed to reset stale retries: {e}")
                return 0

    def _row_to_item(self, row: sqlite3.Row) -> RetryItem:
        """Build the item for a retry_queue row (connection row_factory must be sqlite3.Row)."""
        return RetryItem(
            url=row['url'],
            service_name=row['service_name'],
            error_message=row['error_message'],
            retry_count=row['retry_count'],
            max_retries=row['max_retries'],
            priority=row['priority'],
            created_at=row['created_at'],
            next_retry_at=row['next_retry_at'],
            last_attempt_at=row['last_attempt_at'],
            status=RetryStatus(row['status']),
            metadata=self._safe_json_loads(row['metadata']),
            config=self.config,
        )

    def get_pending_retries(self, limit: int = 50) -> list[RetryItem]:
        """Get pending retry items that are due.

        An empty list means nothing is due; a database failure raises sqlite3.Error.
        """
        with self._lock:
            with self.sqlite_manager.get_connection() as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute("""
                    SELECT * FROM retry_queue
                    WHERE status = 'pending' AND next_retry_at <= ?
                    ORDER BY priority ASC, next_retry_at ASC
                    LIMIT ?
                """, (time.time(), limit)).fetchall()
                return [self._row_to_item(row) for row in rows]

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

    def record_retry_outcome(self, url: str, service_name: str, success: bool,
                             error_message: str | None = None) -> bool:
        """Delete the row on success; on failure reschedule it or dead-letter it.

        Returns whether an in_progress row was updated. False also covers a database
        failure, which is rolled back and leaves the row in_progress for the next pass.
        """
        with self._lock:
            try:
                with self.sqlite_manager.get_connection() as conn:
                    if success:
                        cursor = conn.execute("""
                            DELETE FROM retry_queue
                            WHERE url = ? AND service_name = ? AND status = 'in_progress'
                        """, (url, service_name))
                        recorded = cursor.rowcount > 0
                    else:
                        recorded = self._reschedule_or_dead_letter(conn, url, service_name, error_message)
                    conn.commit()
                    return recorded

            except sqlite3.Error as e:
                self._logger.error(f"Failed to record retry outcome for {url}: {e}")
                return False

    def _reschedule_or_dead_letter(self, conn: sqlite3.Connection, url: str, service_name: str,
                                   error_message: str | None) -> bool:
        """Count a failed attempt on an in_progress row. Returns whether such a row exists."""
        conn.row_factory = sqlite3.Row
        row = conn.execute("""
            SELECT * FROM retry_queue
            WHERE url = ? AND service_name = ? AND status = 'in_progress'
        """, (url, service_name)).fetchone()
        if row is None:
            return False

        retry_item = self._row_to_item(row)
        retry_item.error_message = error_message or retry_item.error_message
        retry_item.increment_retry()

        if retry_item.should_move_to_dead_letter():
            self._move_to_dead_letter_queue(conn, retry_item)
        else:
            conn.execute("""
                UPDATE retry_queue
                SET retry_count = ?, next_retry_at = ?, status = ?,
                    error_message = ?, last_attempt_at = ?
                WHERE url = ? AND service_name = ? AND status = 'in_progress'
            """, (
                retry_item.retry_count,
                retry_item.next_retry_at,
                retry_item.status.value,
                retry_item.error_message,
                retry_item.last_attempt_at,
                url,
                service_name
            ))
        return True

    def _move_to_dead_letter_queue(self, conn: sqlite3.Connection,
                                 retry_item: RetryItem) -> None:
        """Move an item to the dead letter queue. Errors reach the caller, which rolls back."""
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

        conn.execute("""
            DELETE FROM retry_queue WHERE url = ? AND service_name = ?
        """, (retry_item.url, retry_item.service_name))

        self._logger.warning(f"Moved to dead letter queue: {retry_item.url} "
                           f"(service: {retry_item.service_name}, "
                           f"retries: {retry_item.retry_count})")


# Global retry queue instance
_retry_queue = None
_retry_queue_lock = threading.Lock()


def get_retry_queue() -> SQLiteRetryQueue:
    """Get the global retry queue instance, creating its database on first use."""
    global _retry_queue
    if _retry_queue is None:
        with _retry_queue_lock:
            if _retry_queue is None:
                _retry_queue = SQLiteRetryQueue()
    return _retry_queue
