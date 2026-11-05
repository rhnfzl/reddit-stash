"""Tests for the current SQLite retry queue contract."""

import configparser
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from utils.media_services.retry_queue import RetryConfig, RetryItem, SQLiteRetryQueue


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
        self.assertEqual(items[0].metadata['save_path'], '/archive/image.jpg')

    def test_completed_retry_is_removed(self):
        url = 'https://example.com/image.jpg'
        self.queue.add_failed_download(url, 'timeout', 'generic')
        self._make_ready(url)

        self.assertTrue(self.queue.mark_retry_started(url, 'generic'))
        self.queue.record_retry_outcome(url, 'generic', success=True)

        self.assertEqual(self.queue.get_pending_retries(), [])

    def test_failed_retry_is_rescheduled(self):
        url = 'https://example.com/image.jpg'
        self.queue.add_failed_download(url, 'timeout', 'generic')
        self._make_ready(url)

        self.assertTrue(self.queue.mark_retry_started(url, 'generic'))
        self.queue.record_retry_outcome(url, 'generic', success=False, error_message='still unavailable')

        with self.queue.sqlite_manager.get_connection() as connection:
            row = connection.execute(
                'SELECT retry_count, status, error_message FROM retry_queue WHERE url = ?',
                (url,),
            ).fetchone()

        self.assertEqual(tuple(row), (1, 'pending', 'still unavailable'))

    def test_failed_dead_letter_move_rolls_back_and_reports_failure(self):
        url = 'https://example.com/image.jpg'
        self.queue.add_failed_download(url, 'timeout', 'generic', max_retries=1)
        self._make_ready(url)
        self.assertTrue(self.queue.mark_retry_started(url, 'generic'))
        with self.queue.sqlite_manager.get_connection() as connection:
            connection.execute(
                "CREATE TRIGGER block_delete BEFORE DELETE ON retry_queue "
                "BEGIN SELECT RAISE(ABORT, 'blocked'); END"
            )
            connection.commit()

        self.assertFalse(self.queue.record_retry_outcome(url, 'generic', success=False))

        with self.queue.sqlite_manager.get_connection() as connection:
            row = connection.execute('SELECT status, retry_count FROM retry_queue').fetchone()
            dead = connection.execute('SELECT COUNT(*) FROM dead_letter_queue').fetchone()
        self.assertEqual(tuple(row), ('in_progress', 0))
        self.assertEqual(dead[0], 0)

    def test_exhausted_retry_moves_to_dead_letter_queue(self):
        url = 'https://example.com/image.jpg'
        self.queue.add_failed_download(url, 'timeout', 'generic', max_retries=1)
        self._make_ready(url)
        self.assertTrue(self.queue.mark_retry_started(url, 'generic'))

        self.assertTrue(self.queue.record_retry_outcome(url, 'generic', success=False, error_message='gone'))

        with self.queue.sqlite_manager.get_connection() as connection:
            queued = connection.execute('SELECT COUNT(*) FROM retry_queue').fetchone()
            dead = connection.execute('SELECT error_message, retry_count FROM dead_letter_queue').fetchone()
        self.assertEqual(queued[0], 0)
        self.assertEqual(tuple(dead), ('gone', 1))

    def test_explicit_max_retries_of_five_is_kept(self):
        item = RetryItem('u', 'generic', 'e', max_retries=5, config=RetryConfig(max_retries=3))
        self.assertEqual(item.max_retries, 5)
        self.assertEqual(RetryItem('u', 'generic', 'e', config=RetryConfig(max_retries=3)).max_retries, 3)

    def test_item_without_config_uses_the_retry_settings(self):
        settings = configparser.ConfigParser()
        settings.read_dict({'Retry': {'max_retries': '7'}})
        with patch('utils.media_services.retry_queue.load_settings', return_value=settings):
            item = RetryItem('u', 'generic', 'e')
        self.assertEqual(item.config.max_retries, 7)
        self.assertEqual(item.max_retries, 7)

    def test_add_failed_download_reports_whether_it_queued(self):
        url = 'https://example.com/image.jpg'
        self.assertTrue(self.queue.add_failed_download(url, 'timeout', 'generic'))
        self._drop_queue_table()
        self.assertFalse(self.queue.add_failed_download(url, 'timeout', 'generic'))

    def test_in_progress_row_counts_as_queued_and_is_not_replaced(self):
        url = 'https://example.com/image.jpg'
        self.queue.add_failed_download(url, 'timeout', 'generic')
        self._make_ready(url)
        self.assertTrue(self.queue.mark_retry_started(url, 'generic'))

        self.assertTrue(self.queue.add_failed_download(url, 'later error', 'generic'))

        with self.queue.sqlite_manager.get_connection() as connection:
            row = connection.execute('SELECT status, error_message FROM retry_queue').fetchone()
        self.assertEqual(tuple(row), ('in_progress', 'timeout'))

    def test_metadata_that_is_not_an_object_reads_as_empty(self):
        url = 'https://example.com/image.jpg'
        self.queue.add_failed_download(url, 'timeout', 'generic')
        self._make_ready(url)
        with self.queue.sqlite_manager.get_connection() as connection:
            connection.execute("UPDATE retry_queue SET metadata = '[1]'")
            connection.commit()

        self.assertEqual(self.queue.get_pending_retries()[0].metadata, {})

    def test_pending_lookup_raises_on_database_failure(self):
        self._drop_queue_table()
        with self.assertRaises(sqlite3.Error):
            self.queue.get_pending_retries()

    def _drop_queue_table(self):
        with self.queue.sqlite_manager.get_connection() as connection:
            connection.execute('DROP TABLE retry_queue')
            connection.commit()

    def test_custom_database_paths_have_isolated_managers(self):
        second_path = Path(self.temp_dir.name) / 'second.sqlite3'
        second_queue = SQLiteRetryQueue(str(second_path))

        self.assertEqual(self.queue.sqlite_manager.db_path, self.database_path)
        self.assertEqual(second_queue.sqlite_manager.db_path, second_path)
        self.assertIsNot(self.queue.sqlite_manager, second_queue.sqlite_manager)


if __name__ == '__main__':
    unittest.main()
