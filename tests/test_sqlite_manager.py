"""The connection settings must reach SQLite with the intended values."""

import inspect
import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from utils.content_recovery.cache_manager import RecoveryCacheManager
from utils.media_services.retry_queue import SQLiteRetryQueue
from utils.sqlite_manager import ThreadLocalSQLiteManager


class TestPragmaSettings(unittest.TestCase):
    def test_cache_size_is_64_mb_and_mmap_is_128_mb(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = ThreadLocalSQLiteManager(os.path.join(tmp, 'probe.db'))
            with manager.get_connection() as conn:
                cache_size = conn.execute('PRAGMA cache_size').fetchone()[0]
            manager.close_connection()

        # Negative means KiB; a positive value would mean 64000 pages.
        self.assertEqual(cache_size, -64000)
        self.assertEqual(manager.pragma_settings['mmap_size'], 134217728)


class TestDefaultDatabaseNames(unittest.TestCase):
    """A retained working directory may hold these files, so a changed default would orphan their data."""

    def _default_path(self, func, parameter):
        return inspect.signature(func).parameters[parameter].default

    def test_retry_queue_defaults(self):
        self.assertEqual(self._default_path(SQLiteRetryQueue.__init__, 'db_path'), 'reddit_stash_retry_queue.db')

    def test_recovery_cache_defaults(self):
        with patch('utils.content_recovery.cache_manager.get_sqlite_manager',
                   return_value=MagicMock()) as get_manager:
            RecoveryCacheManager()
        get_manager.assert_called_once_with('retry_queue.db')


if __name__ == '__main__':
    unittest.main()
