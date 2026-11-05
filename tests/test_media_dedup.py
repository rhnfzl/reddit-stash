"""Regression tests for content-addressed media downloads."""

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from tests.media_helpers import media_manager
from utils.media_services.media_types import DownloadResult, DownloadStatus


class TestContentHashMediaDedup(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)

        self.manager = media_manager()

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

        with patch("utils.media_services.download_manager.url_transformer.transform", side_effect=transform):
            saved_first_path = self.manager.download_media("https://i.redd.it/first.jpg", first_path).local_path
            saved_second_path = self.manager.download_media("https://i.redd.it/second.jpg", second_path).local_path

        self.assertEqual(saved_first_path, first_path)
        self.assertEqual(saved_second_path, second_path)
        self.assertTrue(os.path.samefile(first_path, second_path))
        self.assertEqual(os.stat(first_path).st_nlink, 2)

    def test_repeated_url_reuses_the_file_from_this_session(self):
        path = os.path.join(self.temp_dir.name, "first.jpg")

        def write_file(_url, destination):
            Path(destination).write_bytes(b"media")
            return DownloadResult(status=DownloadStatus.SUCCESS, local_path=destination, content_hash="hash")

        service = Mock(download=Mock(side_effect=write_file))
        self.manager._get_service_for_url = Mock(return_value=("reddit_image", service))

        def transform(url):
            return SimpleNamespace(url=url, transformed=False, platform="", notes="")

        with patch("utils.media_services.download_manager.url_transformer.transform", side_effect=transform):
            first = self.manager.download_media("https://i.redd.it/first.jpg", path).local_path
            second = self.manager.download_media("https://i.redd.it/first.jpg", path).local_path

        self.assertEqual((first, second), (path, path))
        service.download.assert_called_once()

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

        with patch("utils.media_services.download_manager.url_transformer.transform", side_effect=transform):
            self.manager.download_media("https://i.redd.it/first.jpg", first_path)
            with patch("utils.media_services.download_manager.os.link", side_effect=OSError("cross-device")):
                saved_second_path = self.manager.download_media("https://i.redd.it/second.jpg", second_path).local_path

        self.assertEqual(saved_second_path, second_path)
        self.assertFalse(os.path.samefile(first_path, second_path))
        self.assertEqual(Path(second_path).read_bytes(), b"https://i.redd.it/second.jpg")

    def test_overwriting_a_path_removes_its_stale_hash_mapping(self):
        shared_path = os.path.join(self.temp_dir.name, "shared.jpg")
        duplicate_path = os.path.join(self.temp_dir.name, "duplicate.jpg")
        content_hashes = {
            "https://i.redd.it/first.jpg": "first-hash",
            "https://i.redd.it/second.jpg": "second-hash",
            "https://i.redd.it/repeated-first.jpg": "first-hash",
        }

        def download(url, destination):
            Path(destination).parent.mkdir(parents=True, exist_ok=True)
            Path(destination).write_bytes(url.encode())
            return DownloadResult(
                status=DownloadStatus.SUCCESS,
                local_path=destination,
                content_hash=content_hashes[url],
            )

        self.manager._get_service_for_url = Mock(return_value=("reddit_image", Mock(download=download)))

        def transform(url):
            return SimpleNamespace(url=url, transformed=False, platform="", notes="")

        with patch("utils.media_services.download_manager.url_transformer.transform", side_effect=transform):
            self.manager.download_media("https://i.redd.it/first.jpg", shared_path)
            self.manager.download_media("https://i.redd.it/second.jpg", shared_path)
            self.manager.download_media("https://i.redd.it/repeated-first.jpg", duplicate_path)

        self.assertEqual(Path(shared_path).read_bytes(), b"https://i.redd.it/second.jpg")
        self.assertEqual(Path(duplicate_path).read_bytes(), b"https://i.redd.it/repeated-first.jpg")


if __name__ == "__main__":
    unittest.main()
