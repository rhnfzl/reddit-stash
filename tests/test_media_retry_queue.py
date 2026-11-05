"""Regression tests for persistent media retry destinations."""

import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from tests.media_helpers import media_manager
from utils.media_services.retry_queue import RetryItem
from utils.media_services.media_types import DownloadResult, DownloadStatus


class TestMediaRetryQueue(unittest.TestCase):
    def setUp(self):
        self.manager = media_manager()
        self.manager._generic_downloader = Mock()
        self.manager._reddit_downloader = Mock()
        self.manager._imgur_downloader = Mock()

    def test_failed_download_persists_its_destination(self):
        url = 'https://i.redd.it/image.png'
        save_path = '/archive/post/image.png'
        downloader = Mock()
        self.manager._get_service_for_url = Mock(return_value=('reddit_image', downloader))
        downloader.download.return_value = DownloadResult(
            status=DownloadStatus.FAILED,
            error_message='timeout',
        )

        transform = SimpleNamespace(url=url, transformed=False, platform='', notes='')
        with patch('utils.media_services.download_manager.url_transformer.transform', return_value=transform):
            result = self.manager.download_media(url, save_path)

        self.assertIsNone(result.local_path)
        self.manager._retry_queue.add_failed_download.assert_called_once_with(
            url,
            'timeout',
            'reddit_image',
            metadata={'save_path': save_path},
        )

    def test_retry_uses_persisted_destination(self):
        url = 'https://i.redd.it/image.png'
        save_path = '/archive/post/image.png'
        self.manager._retry_queue.get_pending_retries.return_value = [
            RetryItem(url, 'reddit_image', 'timeout', metadata={'save_path': save_path}),
        ]
        self.manager._retry_queue.mark_retry_started.return_value = True
        self.manager._fetch = Mock(return_value=(
            DownloadResult(status=DownloadStatus.SUCCESS, local_path=save_path),
            'reddit_image',
        ))

        stats = self.manager.process_pending_retries()

        self.assertEqual(stats, {'processed': 1, 'successful': 1, 'failed': 0, 'skipped': 0, 'unlinked': 0})
        self.manager._fetch.assert_called_once_with(url, save_path)

    def test_retry_without_persisted_destination_stays_pending(self):
        url = 'https://i.redd.it/image.png'
        self.manager._retry_queue.get_pending_retries.return_value = [
            RetryItem(url, 'reddit_image', 'timeout'),
        ]
        self.manager._retry_queue.mark_retry_started.return_value = True
        self.manager._fetch = Mock()

        stats = self.manager.process_pending_retries()

        self.assertEqual(stats, {'processed': 0, 'successful': 0, 'failed': 1, 'skipped': 0, 'unlinked': 0})
        self.manager._fetch.assert_not_called()
        self.manager._retry_queue.record_retry_outcome.assert_called_once_with(
            url,
            'reddit_image',
            success=False,
            error_message='Legacy retry item has no persisted save path',
        )


if __name__ == '__main__':
    unittest.main()
