"""Regression tests for persistent media retry destinations."""

import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from utils.media_download_manager import MediaDownloadManager
from utils.service_abstractions import DownloadResult, DownloadStatus


class TestMediaRetryQueue(unittest.TestCase):
    def setUp(self):
        self.manager = MediaDownloadManager.__new__(MediaDownloadManager)
        self.manager._media_config = Mock()
        self.manager._media_config.is_images_enabled.return_value = True
        self.manager._logger = Mock()
        self.manager._url_lock = threading.Lock()
        self.manager._permanent_failures = set()
        self.manager._transient_failures = {}
        self.manager._downloaded_urls = {}
        self.manager._retry_queue = Mock()
        self.manager._recovery_service = Mock()
        self.manager._recovery_service.is_enabled.return_value = False
        self.manager._service_manager = Mock()

    def test_failed_download_persists_its_destination(self):
        url = 'https://i.redd.it/image.png'
        save_path = '/archive/post/image.png'
        self.manager._get_service_for_url = Mock(return_value=('reddit_image', Mock()))
        self.manager._service_manager.execute_with_protection.return_value = DownloadResult(
            status=DownloadStatus.FAILED,
            error_message='timeout',
        )

        transform = SimpleNamespace(url=url, transformed=False, platform='', notes='')
        with patch('utils.media_download_manager.url_transformer.transform', return_value=transform):
            result = self.manager.download_media(url, save_path)

        self.assertIsNone(result)
        self.manager._retry_queue.add_failed_download.assert_called_once_with(
            url,
            'timeout',
            'reddit_image',
            metadata={'save_path': save_path},
        )

    def test_retry_uses_persisted_destination(self):
        url = 'https://i.redd.it/image.png'
        save_path = '/archive/post/image.png'
        self.manager._retry_queue.get_pending_retries.return_value = [{
            'id': 7,
            'url': url,
            'service_name': 'reddit_image',
            'metadata': {'save_path': save_path},
        }]
        self.manager._retry_queue.mark_retry_started.return_value = True
        self.manager.download_media = Mock(return_value=save_path)

        stats = self.manager.process_pending_retries()

        self.assertEqual(stats, {'processed': 1, 'successful': 1, 'failed': 0, 'skipped': 0})
        self.manager.download_media.assert_called_once_with(url, save_path)

    def test_retry_without_persisted_destination_stays_pending(self):
        url = 'https://i.redd.it/image.png'
        self.manager._retry_queue.get_pending_retries.return_value = [{
            'id': 7,
            'url': url,
            'service_name': 'reddit_image',
            'metadata': {},
        }]
        self.manager._retry_queue.mark_retry_started.return_value = True
        self.manager.download_media = Mock()

        stats = self.manager.process_pending_retries()

        self.assertEqual(stats, {'processed': 0, 'successful': 0, 'failed': 1, 'skipped': 0})
        self.manager.download_media.assert_not_called()
        self.manager._retry_queue.mark_retry_completed.assert_called_once_with(
            url,
            success=False,
            error_message='Retry item has no persisted save path',
        )


if __name__ == '__main__':
    unittest.main()
