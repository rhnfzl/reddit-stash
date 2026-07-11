"""Tests for the current SQLite retry queue contract."""

import tempfile
import time
import unittest
from pathlib import Path

from utils.retry_queue import SQLiteRetryQueue


class TestSQLiteRetryQueue(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_dir.name) / 'retry.sqlite3'
        self.queue = SQLiteRetryQueue(str(self.database_path))

    def tearDown(self):
        self.temp_dir.cleanup()

    def _make_ready(self, url):
        with self.queue.sqlite_manager.get_connection() as connection:
            connection.execute(
                'UPDATE retry_queue SET next_retry_at = ? WHERE url = ?',
                (time.time() - 1, url),
            )
            connection.commit()

    def test_failed_download_persists_metadata(self):
        url = 'https://example.com/image.jpg'
        self.queue.add_failed_download(
            url,
            'timeout',
            'generic',
            metadata={'save_path': '/archive/image.jpg'},
        )
        self._make_ready(url)

        items = self.queue.get_pending_retries()

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['metadata']['save_path'], '/archive/image.jpg')

    def test_completed_retry_is_removed(self):
        url = 'https://example.com/image.jpg'
        self.queue.add_failed_download(url, 'timeout', 'generic')
        self._make_ready(url)

        self.assertTrue(self.queue.mark_retry_started(url, 'generic'))
        self.queue.mark_retry_completed(url, success=True)

        self.assertEqual(self.queue.get_pending_retries(), [])

    def test_failed_retry_is_rescheduled(self):
        url = 'https://example.com/image.jpg'
        self.queue.add_failed_download(url, 'timeout', 'generic')
        self._make_ready(url)

        self.assertTrue(self.queue.mark_retry_started(url, 'generic'))
        self.queue.mark_retry_completed(url, success=False, error_message='still unavailable')

        with self.queue.sqlite_manager.get_connection() as connection:
            row = connection.execute(
                'SELECT retry_count, status, error_message FROM retry_queue WHERE url = ?',
                (url,),
            ).fetchone()

        self.assertEqual(tuple(row), (1, 'pending', 'still unavailable'))

    def test_custom_database_paths_have_isolated_managers(self):
        second_path = Path(self.temp_dir.name) / 'second.sqlite3'
        second_queue = SQLiteRetryQueue(str(second_path))

        self.assertEqual(self.queue.sqlite_manager.db_path, self.database_path)
        self.assertEqual(second_queue.sqlite_manager.db_path, second_path)
        self.assertIsNot(self.queue.sqlite_manager, second_queue.sqlite_manager)


if __name__ == '__main__':
    unittest.main()
