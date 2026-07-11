"""Regression tests for content-addressed media downloads."""

import os
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from utils.media_download_manager import MediaDownloadManager
from utils.service_abstractions import DownloadResult, DownloadStatus


class TestContentHashMediaDedup(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)

        self.manager = MediaDownloadManager.__new__(MediaDownloadManager)
        self.manager._media_config = Mock()
        self.manager._media_config.is_images_enabled.return_value = True
        self.manager._logger = Mock()
        self.manager._url_lock = threading.Lock()
        self.manager._permanent_failures = set()
        self.manager._transient_failures = {}
        self.manager._downloaded_urls = {}
        self.manager._downloaded_content_hashes = {}
        self.manager._retry_queue = Mock()
        self.manager._recovery_service = Mock()
        self.manager._recovery_service.is_enabled.return_value = False
        self.manager._service_manager = Mock()
        self.manager._service_manager.execute_with_protection.side_effect = (
            lambda _service, operation, fallback_value: operation()
        )

    def test_identical_media_is_hardlinked_to_the_first_download(self):
        content_hash = "same-content-hash"
        first_path = os.path.join(self.temp_dir.name, "first.jpg")
        second_path = os.path.join(self.temp_dir.name, "second.jpg")

        def download(_url, destination):
            Path(destination).parent.mkdir(parents=True, exist_ok=True)
            Path(destination).write_bytes(b"identical media")
            return DownloadResult(
                status=DownloadStatus.SUCCESS,
                local_path=destination,
                content_hash=content_hash,
            )

        self.manager._get_service_for_url = Mock(return_value=("reddit_image", Mock(download=download)))
        def transform(url):
            return SimpleNamespace(url=url, transformed=False, platform="", notes="")

        with patch("utils.media_download_manager.url_transformer.transform", side_effect=transform):
            saved_first_path = self.manager.download_media("https://i.redd.it/first.jpg", first_path)
            saved_second_path = self.manager.download_media("https://i.redd.it/second.jpg", second_path)

        self.assertEqual(saved_first_path, first_path)
        self.assertEqual(saved_second_path, second_path)
        self.assertTrue(os.path.samefile(first_path, second_path))
        self.assertEqual(os.stat(first_path).st_nlink, 2)

    def test_hardlink_failure_keeps_the_second_download(self):
        first_path = os.path.join(self.temp_dir.name, "first.jpg")
        second_path = os.path.join(self.temp_dir.name, "second.jpg")

        def download(url, destination):
            Path(destination).parent.mkdir(parents=True, exist_ok=True)
            Path(destination).write_bytes(url.encode())
            return DownloadResult(
                status=DownloadStatus.SUCCESS,
                local_path=destination,
                content_hash="same-content-hash",
            )

        self.manager._get_service_for_url = Mock(return_value=("reddit_image", Mock(download=download)))

        def transform(url):
            return SimpleNamespace(url=url, transformed=False, platform="", notes="")

        with patch("utils.media_download_manager.url_transformer.transform", side_effect=transform):
            self.manager.download_media("https://i.redd.it/first.jpg", first_path)
            with patch("utils.media_download_manager.os.link", side_effect=OSError("cross-device")):
                saved_second_path = self.manager.download_media("https://i.redd.it/second.jpg", second_path)

        self.assertEqual(saved_second_path, second_path)
        self.assertFalse(os.path.samefile(first_path, second_path))
        self.assertEqual(Path(second_path).read_bytes(), b"https://i.redd.it/second.jpg")


if __name__ == "__main__":
    unittest.main()
