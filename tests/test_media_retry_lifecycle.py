"""The retry policy and the life of a retry row across runs."""

import contextlib
import io
import os
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

import reddit_stash
from tests.media_helpers import URL, failed_download, media_manager
from utils.media_services.error_isolation import CircuitState
from utils.media_services.retry_queue import SQLiteRetryQueue
from utils.media_services.media_types import DownloadResult, DownloadStatus


class TestRetryPolicy(unittest.TestCase):
    def _download(self, status, queued=True):
        manager = media_manager()
        manager._logger = Mock()
        manager._retry_queue.add_failed_download.return_value = queued
        downloader = Mock()
        downloader.download.return_value = failed_download(status)
        manager._get_service_for_url = Mock(return_value=('imgur', downloader))
        with patch.object(manager, '_resolve_download_url', return_value=URL):
            self.assertIsNone(manager.download_media(URL, '/x').local_path)
        return manager

    def test_permanent_failures_are_not_queued(self):
        for status in (DownloadStatus.NOT_FOUND, DownloadStatus.INVALID_URL):
            with self.subTest(status=status):
                self._download(status)._retry_queue.add_failed_download.assert_not_called()

    def test_retryable_failures_are_queued(self):
        for status in (DownloadStatus.FAILED, DownloadStatus.RATE_LIMITED, DownloadStatus.REJECTED,
                       DownloadStatus.FORBIDDEN):
            with self.subTest(status=status):
                self._download(status)._retry_queue.add_failed_download.assert_called_once()

    def test_lost_enqueue_is_logged(self):
        manager = self._download(DownloadStatus.FAILED, queued=False)

        manager._logger.error.assert_called_once()


class TestRetryRowLifecycle(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.queue = SQLiteRetryQueue(os.path.join(self.tmp.name, 'q.db'))
        self.saved = os.path.join(self.tmp.name, 'abc.jpg')
        self.queue.add_failed_download(URL, 'timeout', 'generic', metadata={'save_path': self.saved})
        with self.queue.sqlite_manager.get_connection() as conn:
            conn.execute('UPDATE retry_queue SET next_retry_at = 0')
            conn.commit()
        self.manager = media_manager(retry_queue=self.queue)
        self.manager._generic_downloader = Mock()
        self.manager._reddit_downloader = Mock()
        self.manager._imgur_downloader = Mock()

    def _row(self):
        with self.queue.sqlite_manager.get_connection() as conn:
            return conn.execute('SELECT status, retry_count FROM retry_queue WHERE url = ?', (URL,)).fetchone()

    def test_url_cached_this_session_completes_its_row(self):
        with open(self.saved, 'wb') as f:
            f.write(b'media')
        self.manager._downloaded_urls[URL] = self.saved
        recovered = Mock()

        stats = self.manager.process_pending_retries(on_recovered=recovered)

        self.assertEqual(stats['successful'], 1)
        recovered.assert_called_once_with(URL, self.saved)
        self.assertIsNone(self._row())

    def test_failed_media_init_does_not_use_up_an_attempt(self):
        self.manager._fetch = Mock()
        for name in ('_generic_downloader', '_reddit_downloader', '_imgur_downloader'):
            with self.subTest(name), patch.object(self.manager, name, None):
                stats = self.manager.process_pending_retries()

                self.manager._fetch.assert_not_called()
                self.assertEqual(stats['skipped'], 1)
                self.assertEqual(tuple(self._row()), ('pending', 0))

    def _run_pass(self):
        out = io.StringIO()
        with patch.object(reddit_stash, 'get_retry_queue', return_value=self.queue), \
                patch.object(reddit_stash, 'get_media_manager', return_value=self.manager), \
                patch.object(reddit_stash, 'relink_media_line', return_value=False), \
                contextlib.redirect_stdout(out):
            reddit_stash.retry_pending_media()
        return out.getvalue()

    def test_summary_says_when_a_failed_start_skipped_the_pass(self):
        self.manager._imgur_downloader = None
        self.assertIn('1 queued retries skipped', self._run_pass())

    def test_summary_says_when_a_recovered_file_stays_unlinked(self):
        self.manager._fetch = Mock(return_value=(
            DownloadResult(status=DownloadStatus.SUCCESS, local_path=self.saved), 'generic',
        ))
        self.assertIn('1 recovered file(s) saved', self._run_pass())

    def test_lone_in_progress_row_is_retried_by_the_next_run(self):
        self.assertTrue(self.queue.mark_retry_started(URL, 'generic'))
        self.manager._fetch = Mock(return_value=(failed_download(), 'generic'))

        self._run_pass()

        self.manager._fetch.assert_called_once_with(URL, self.saved)
        self.assertEqual(tuple(self._row()), ('pending', 1))

    def test_failed_downloader_start_queues_new_download(self):
        queue = Mock(spec=SQLiteRetryQueue)
        manager = media_manager(retry_queue=queue)
        url = 'https://i.imgur.com/abc.jpg'
        with patch.object(manager, '_resolve_download_url', return_value=url):
            self.assertIsNone(manager.download_media(url, self.saved).local_path)

        queue.add_failed_download.assert_called_once_with(
            url, 'No suitable downloader', 'imgur', metadata={'save_path': self.saved},
        )

    def test_recovered_file_without_a_relinked_line_is_counted(self):
        self.manager._fetch = Mock(return_value=(
            DownloadResult(status=DownloadStatus.SUCCESS, local_path=self.saved), 'generic',
        ))

        stats = self.manager.process_pending_retries(on_recovered=Mock(return_value=False))

        self.assertEqual((stats['successful'], stats['unlinked']), (1, 1))

    def test_failed_retry_uses_one_attempt_and_is_not_requeued(self):
        self.manager._fetch = Mock(return_value=(failed_download(message='still down'), 'generic'))

        self.manager.process_pending_retries()

        self.assertEqual(tuple(self._row()), ('pending', 1))

    def test_open_breaker_does_not_use_up_an_attempt(self):
        breaker = self.manager._breaker('generic')
        breaker.stats.state = CircuitState.OPEN
        breaker.stats.opened_at = time.time()
        self.manager._fetch = Mock()

        stats = self.manager.process_pending_retries()

        self.assertEqual(stats['skipped'], 1)
        self.manager._fetch.assert_not_called()
        self.assertEqual(tuple(self._row()), ('pending', 0))

    def test_limit_caps_the_rows_retried_in_one_pass(self):
        for name in ('second', 'third'):
            self.queue.add_failed_download(f'https://example.com/{name}.jpg', 'timeout', 'generic',
                                           metadata={'save_path': os.path.join(self.tmp.name, f'{name}.jpg')})
        with self.queue.sqlite_manager.get_connection() as conn:
            conn.execute('UPDATE retry_queue SET next_retry_at = 0')
            conn.commit()
        self.manager._fetch = Mock(return_value=(failed_download(message='still down'), 'generic'))

        self.manager.process_pending_retries(limit=2)

        self.assertEqual(self.manager._fetch.call_count, 2)

    def test_media_disabled_leaves_rows_alone(self):
        self.manager._media_config.is_media_enabled.return_value = False
        self.manager._fetch = Mock()

        self.manager.process_pending_retries()

        self.manager._fetch.assert_not_called()
        self.assertEqual(tuple(self._row()), ('pending', 0))

    def test_completion_only_touches_the_named_service_and_reports_it(self):
        self.queue.add_failed_download(URL, 'timeout', 'imgur')
        with self.queue.sqlite_manager.get_connection() as conn:
            conn.execute("UPDATE retry_queue SET status = 'in_progress'")
            conn.commit()

        self.assertTrue(self.queue.record_retry_outcome(URL, 'generic', success=True))
        self.assertFalse(self.queue.record_retry_outcome(URL, 'generic', success=True))

        with self.queue.sqlite_manager.get_connection() as conn:
            rows = conn.execute('SELECT service_name FROM retry_queue').fetchall()
        self.assertEqual([row[0] for row in rows], ['imgur'])

    def test_failed_completion_only_touches_the_named_service(self):
        self.queue.add_failed_download(URL, 'timeout', 'imgur')
        with self.queue.sqlite_manager.get_connection() as conn:
            conn.execute("UPDATE retry_queue SET status = 'in_progress'")
            conn.execute("UPDATE retry_queue SET retry_count = 2 WHERE service_name = 'generic'")
            conn.commit()

        self.assertTrue(self.queue.record_retry_outcome(URL, 'imgur', success=False, error_message='boom'))

        with self.queue.sqlite_manager.get_connection() as conn:
            rows = conn.execute('SELECT service_name, retry_count, status FROM retry_queue').fetchall()
        self.assertEqual(
            {row[0]: (row[1], row[2]) for row in rows},
            {'generic': (2, 'in_progress'), 'imgur': (1, 'pending')},
        )


if __name__ == '__main__':
    unittest.main()
