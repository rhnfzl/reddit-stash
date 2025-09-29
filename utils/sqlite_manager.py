"""
Thread-safe SQLite connection manager for Reddit Stash.

This module implements 2024-2025 best practices for SQLite threading safety:
- Thread-local connections to ensure each thread has its own connection
- WAL mode for better concurrency
- Proper connection pooling and cleanup
- Optimized PRAGMA settings for performance
"""

import sqlite3
import threading
import logging
import time
import contextlib
from typing import Dict, Any, Optional, Generator
from pathlib import Path


class ThreadLocalSQLiteManager:
    """
    Thread-safe SQLite connection manager using thread-local storage.

    This implementation follows 2024-2025 best practices:
    - Each thread maintains its own database connection
    - WAL mode enabled for concurrent read/write operations
    - Optimized PRAGMA settings for performance
    - Automatic connection cleanup and optimization
    """

    def __init__(self, db_path: str, timeout: int = 30):
        self.db_path = Path(db_path)
        self.timeout = timeout
        self._local = threading.local()
        self._lock = threading.RLock()
        self._logger = logging.getLogger(f"{__name__}.{self.__class__.__name__}")

        # Connection settings optimized for 2024-2025 performance
        self.pragma_settings = {
            'journal_mode': 'WAL',
            'synchronous': 'NORMAL',
            'cache_size': -64000,  # 64MB cache
            'temp_store': 'MEMORY',
            'mmap_size': 134217728,  # 128MB mmap
            'foreign_keys': 'ON',
            'optimize': None  # Will run PRAGMA optimize periodically
        }

    def _get_connection(self) -> sqlite3.Connection:
        """Get thread-local SQLite connection with optimized settings."""
        if not hasattr(self._local, 'connection') or self._local.connection is None:
            self._logger.debug(f"Creating new SQLite connection for thread {threading.current_thread().ident}")

            # Create connection with optimized settings
            conn = sqlite3.connect(
                str(self.db_path),
                timeout=self.timeout,
                check_same_thread=False  # Allow sharing across threads (handled by thread-local storage)
            )

            # Apply optimized PRAGMA settings
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

    def execute_query(self, query: str, params: tuple = ()) -> sqlite3.Cursor:
        """Execute a query with automatic transaction handling."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(query, params)
            conn.commit()
            return cursor

    def execute_many(self, query: str, params_list: list) -> sqlite3.Cursor:
        """Execute multiple queries with automatic transaction handling."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.executemany(query, params_list)
            conn.commit()
            return cursor

    def fetch_one(self, query: str, params: tuple = ()) -> Optional[tuple]:
        """Execute query and fetch one result."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(query, params)
            return cursor.fetchone()

    def fetch_all(self, query: str, params: tuple = ()) -> list:
        """Execute query and fetch all results."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(query, params)
            return cursor.fetchall()

    def close_connection(self):
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

    def vacuum_database(self) -> bool:
        """Vacuum database to reclaim space and optimize."""
        try:
            with self.get_connection() as conn:
                # Temporarily switch to DELETE mode for VACUUM
                conn.execute("PRAGMA journal_mode=DELETE")
                conn.execute("VACUUM")
                # Switch back to WAL mode
                conn.execute("PRAGMA journal_mode=WAL")
                self._logger.info("Database vacuum completed successfully")
                return True
        except sqlite3.Error as e:
            self._logger.error(f"Failed to vacuum database: {e}")
            return False

    def get_connection_info(self) -> Dict[str, Any]:
        """Get information about current connection and database."""
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()

                info = {}

                # Basic connection info
                info['thread_id'] = threading.current_thread().ident
                info['database_path'] = str(self.db_path)

                # PRAGMA information
                pragma_queries = [
                    'journal_mode', 'synchronous', 'cache_size',
                    'page_count', 'page_size', 'encoding',
                    'foreign_keys', 'temp_store'
                ]

                for pragma in pragma_queries:
                    cursor.execute(f"PRAGMA {pragma}")
                    result = cursor.fetchone()
                    info[pragma] = result[0] if result else None

                # Calculate database size
                if info.get('page_count') and info.get('page_size'):
                    info['size_bytes'] = info['page_count'] * info['page_size']
                    info['size_mb'] = info['size_bytes'] / (1024 * 1024)

                return info

        except sqlite3.Error as e:
            self._logger.error(f"Failed to get connection info: {e}")
            return {'error': str(e)}

    def __del__(self):
        """Cleanup when manager is destroyed."""
        self.close_connection()


# Global instances for different databases
_retry_queue_manager: Optional[ThreadLocalSQLiteManager] = None
_cache_manager: Optional[ThreadLocalSQLiteManager] = None


def get_retry_queue_manager(db_path: str = "reddit_stash_retry_queue.db") -> ThreadLocalSQLiteManager:
    """Get global retry queue SQLite manager."""
    global _retry_queue_manager
    if _retry_queue_manager is None:
        _retry_queue_manager = ThreadLocalSQLiteManager(db_path)
    return _retry_queue_manager


def get_cache_manager(db_path: str = "retry_queue.db") -> ThreadLocalSQLiteManager:
    """Get global cache SQLite manager."""
    global _cache_manager
    if _cache_manager is None:
        _cache_manager = ThreadLocalSQLiteManager(db_path)
    return _cache_manager