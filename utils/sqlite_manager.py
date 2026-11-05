"""
Thread-safe SQLite connection manager for Reddit Stash.

Gives each thread its own connection, in WAL mode, with shared PRAGMA settings.
"""

import sqlite3
import threading
import logging
import time
import contextlib
from collections.abc import Generator
from pathlib import Path

from utils.constants import SQLITE_CACHE_SIZE_KB, SQLITE_MMAP_SIZE_BYTES


class ThreadLocalSQLiteManager:
    """
    Thread-safe SQLite connection manager using thread-local storage.

    Each thread keeps its own connection, in WAL mode so reads and writes can overlap.
    """

    def __init__(self, db_path: str, timeout: int = 30) -> None:
        self.db_path = Path(db_path)
        self.timeout = timeout
        self._local = threading.local()
        self._lock = threading.RLock()
        self._logger = logging.getLogger(f"{__name__}.{self.__class__.__name__}")

        self.pragma_settings = {
            'journal_mode': 'WAL',
            'synchronous': 'NORMAL',
            'cache_size': -SQLITE_CACHE_SIZE_KB,  # negative means KiB, not pages
            'temp_store': 'MEMORY',
            'mmap_size': SQLITE_MMAP_SIZE_BYTES,
            'foreign_keys': 'ON',
            'optimize': None  # Will run PRAGMA optimize periodically
        }

    def _get_connection(self) -> sqlite3.Connection:
        """Get thread-local SQLite connection with the PRAGMA settings applied."""
        if not hasattr(self._local, 'connection') or self._local.connection is None:
            self._logger.debug(f"Creating new SQLite connection for thread {threading.current_thread().ident}")

            conn = sqlite3.connect(
                str(self.db_path),
                timeout=self.timeout,
                check_same_thread=False  # Allow sharing across threads (handled by thread-local storage)
            )

            cursor = conn.cursor()
            for pragma, value in self.pragma_settings.items():
                if value is not None:
                    cursor.execute(f"PRAGMA {pragma}={value}")
                else:
                    cursor.execute(f"PRAGMA {pragma}")

            # Verify WAL mode is enabled
            cursor.execute("PRAGMA journal_mode")
            journal_mode = cursor.fetchone()[0]
            if journal_mode.upper() != 'WAL':
                self._logger.warning(f"Failed to enable WAL mode, using {journal_mode}")
            else:
                self._logger.debug("WAL mode enabled successfully")

            self._local.connection = conn
            self._local.last_optimize = time.time()

        return self._local.connection

    @contextlib.contextmanager
    def get_connection(self) -> Generator[sqlite3.Connection, None, None]:
        """
        Context manager for getting thread-local SQLite connection.

        Usage:
            with sqlite_manager.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT * FROM table")
                result = cursor.fetchall()
        """
        conn = self._get_connection()
        try:
            yield conn
        except Exception as e:
            # Rollback on error
            conn.rollback()
            self._logger.error(f"SQLite operation failed, rolling back: {e}")
            raise
        finally:
            # Periodic optimization (every 5 minutes)
            if hasattr(self._local, 'last_optimize'):
                if time.time() - self._local.last_optimize > 300:  # 5 minutes
                    try:
                        conn.execute("PRAGMA optimize")
                        self._local.last_optimize = time.time()
                        self._logger.debug("Performed periodic database optimization")
                    except sqlite3.Error as e:
                        self._logger.warning(f"Failed to optimize database: {e}")

    def close_connection(self) -> None:
        """Close thread-local connection."""
        if hasattr(self._local, 'connection') and self._local.connection is not None:
            try:
                # Final optimization before closing
                self._local.connection.execute("PRAGMA optimize")
                self._local.connection.close()
                self._logger.debug(f"Closed SQLite connection for thread {threading.current_thread().ident}")
            except sqlite3.Error as e:
                self._logger.warning(f"Error closing SQLite connection: {e}")
            finally:
                self._local.connection = None

    def __del__(self) -> None:
        """Cleanup when manager is destroyed."""
        self.close_connection()


_managers: dict[str, ThreadLocalSQLiteManager] = {}
_managers_lock = threading.Lock()


def get_sqlite_manager(db_path: str) -> ThreadLocalSQLiteManager:
    """Get the shared SQLite manager for a database path."""
    with _managers_lock:
        manager = _managers.get(db_path)
        if manager is None:
            manager = ThreadLocalSQLiteManager(db_path)
            _managers[db_path] = manager
        return manager
