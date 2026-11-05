"""Download branches that decide what is kept on disk and how failures are reported."""

import errno
import io
import os
import tempfile
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from PIL import Image

from tests.media_helpers import FakeResponse
from utils.constants import MAX_URL_LENGTH, MIN_MEDIA_FILE_SIZE
from utils.media_services.base_downloader import BaseHTTPDownloader
from utils.media_services.reddit_media import RedditMediaDownloader
from utils.media_services.safe_http import ConnectionError, get_with_safe_redirects
from utils.media_services.media_types import DownloadResult, DownloadStatus, ServiceConfig

URL = 'https://media.example.com/file.bin'
GOOD_BODY = b'n' * (MIN_MEDIA_FILE_SIZE + 1)
OLD_BODY = b'previously saved file'


class DownloadCase(unittest.TestCase):
    def setUp(self):
        self.downloader = BaseHTTPDownloader(ServiceConfig(name='branch-test', max_file_size=1024 * 1024))
        self.addCleanup(self.downloader.close)
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.dir = temp.name
        self.path = os.path.join(self.dir, 'saved.mp4')

    def download(self, response):
        with patch.object(self.downloader, '_respect_rate_limit'), \
                patch.object(self.downloader, '_get_with_safe_redirects', return_value=(response, URL)):
            return self.downloader.download_file(URL, self.path)


class TestExistingFileSurvivesFailedDownload(DownloadCase):
    def assert_old_file_kept(self, result, status):
        self.assertEqual(result.status, status)
        with open(self.path, 'rb') as saved:
            self.assertEqual(saved.read(), OLD_BODY)
        self.assertEqual(os.listdir(self.dir), [os.path.basename(self.path)])

    def setUp(self):
        super().setUp()
        with open(self.path, 'wb') as saved:
            saved.write(OLD_BODY)

    def test_corrupt_image(self):
        png_path = os.path.join(self.dir, 'saved.png')
        os.rename(self.path, png_path)
        self.path = png_path
        result = self.download(FakeResponse(GOOD_BODY, content_type='image/png'))
        self.assert_old_file_kept(result, DownloadStatus.FAILED)

    def test_too_small(self):
        result = self.download(FakeResponse(b'x'))
        self.assert_old_file_kept(result, DownloadStatus.FAILED)

    def test_oversize_while_streaming(self):
        self.downloader.config = replace(self.downloader.config, max_file_size=10)
        result = self.download(FakeResponse(GOOD_BODY))
        self.assert_old_file_kept(result, DownloadStatus.REJECTED)

    def test_error_while_streaming(self):
        result = self.download(FakeResponse(GOOD_BODY, error=OSError(errno.EIO, 'read failed')))
        self.assert_old_file_kept(result, DownloadStatus.FAILED)

    def test_success_replaces_the_file(self):
        result = self.download(FakeResponse(GOOD_BODY))

        self.assertEqual(result.status, DownloadStatus.SUCCESS)
        self.assertEqual(result.local_path, self.path)
        with open(self.path, 'rb') as saved:
            self.assertEqual(saved.read(), GOOD_BODY)
        self.assertEqual(os.listdir(self.dir), [os.path.basename(self.path)])


class TestUnusableNewDownload(DownloadCase):
    def test_nothing_is_left_behind(self):
        result = self.download(FakeResponse(b'x'))

        self.assertEqual(result.status, DownloadStatus.FAILED)
        self.assertEqual(os.listdir(self.dir), [])

    def test_valid_image_is_kept(self):
        image = io.BytesIO()
        Image.new('RGB', (64, 64), 'red').save(image, 'BMP')
        result = self.download(FakeResponse(image.getvalue(), content_type='image/bmp'))

        self.assertEqual(result.status, DownloadStatus.SUCCESS)


class TestOsErrorClassification(DownloadCase):
    def classify(self, code):
        with patch.object(self.downloader, '_stream_to_file', side_effect=OSError(code, 'disk')), \
                patch('utils.media_services.base_downloader.rate_limit_manager') as limiter:
            result = self.download(FakeResponse(GOOD_BODY))
        return result, limiter.report_response.call_args.args[1]

    def test_no_space(self):
        result, reported = self.classify(errno.ENOSPC)
        self.assertEqual(result.status, DownloadStatus.FAILED)
        self.assertIn('No space left on device', result.error_message)
        self.assertEqual(reported, 507)

    def test_permission_denied(self):
        result, reported = self.classify(errno.EACCES)
        self.assertEqual(result.status, DownloadStatus.FAILED)
        self.assertIn('Permission denied', result.error_message)
        self.assertEqual(reported, 403)

    def test_other_disk_error(self):
        result, reported = self.classify(errno.EIO)
        self.assertIn('File system error', result.error_message)
        self.assertEqual(reported, 500)


class TestValidateUrl(DownloadCase):
    def test_accepted(self):
        for url in (
            'https://media.example.com/a.jpg',
            'https://v.redd.it:443/abc',
            'https://media.example.com:8443/a.jpg',
            'https://i.redd.it',
            'https://i.redd.it:443',
        ):
            with self.subTest(url=url):
                self.assertTrue(self.downloader._validate_url(url))

    def test_rejected(self):
        for url in (
            'https://',
            'https://example.com/' + 'a' * MAX_URL_LENGTH,
            'https://media.example.com',
            'https://media.example.com:443',
            'https://media.example.com:443/',
            'https://media.example.com:port/a.jpg',
            'https://media.example.com:99999/a.jpg',
            'https://user:secret@media.example.com/a.jpg',
            'https://user:secret@media.example.com:443/a.jpg',
            'https://nodots:443/a.jpg',
        ):
            with self.subTest(url=url):
                self.assertFalse(self.downloader._validate_url(url))


class TestPinningGuard(unittest.TestCase):
    def test_url_that_cannot_be_pinned_is_refused_as_a_passing_failure(self):
        validator = MagicMock()
        validator.is_safe_for_download.return_value = True
        validator.resolve_public_addresses.return_value = ()
        session = MagicMock()

        with patch('utils.media_services.safe_http.get_url_validator', return_value=validator):
            with self.assertRaises(ConnectionError):
                get_with_safe_redirects(session, URL, {}, 2)

        session.get.assert_not_called()


class TestRedditMedia(unittest.TestCase):
    def setUp(self):
        self.downloader = RedditMediaDownloader()
        self.addCleanup(self.downloader.close)

    def test_video_404_with_audio_falls_back_to_other_qualities(self):
        fallback = DownloadResult(status=DownloadStatus.NOT_FOUND, error_message='no quality left')
        results = {
            'video': DownloadResult(status=DownloadStatus.NOT_FOUND),
            'audio': DownloadResult(status=DownloadStatus.SUCCESS),
        }
        with patch.object(self.downloader, 'download_file', side_effect=lambda url, path: results[url]), \
                patch.object(self.downloader, '_try_dash_qualities', return_value=fallback) as qualities:
            result = self.downloader._download_video_with_audio('video', 'audio', 'saved.mp4')

        qualities.assert_called_once_with('video', 'saved.mp4')
        self.assertIs(result, fallback)

    def test_merge_clears_the_hash_of_the_video_stream(self):
        with tempfile.TemporaryDirectory() as directory:
            video_path = os.path.join(directory, 'video.mp4')
            audio_path = os.path.join(directory, 'audio.mp4')
            for path in (video_path, audio_path):
                with open(path, 'wb') as stream:
                    stream.write(b'stream')
            video = DownloadResult(
                status=DownloadStatus.SUCCESS, local_path=video_path, content_hash='video-hash', bytes_downloaded=6
            )
            audio = DownloadResult(status=DownloadStatus.SUCCESS, local_path=audio_path)
            merged = DownloadResult(status=DownloadStatus.SUCCESS, bytes_downloaded=12)

            with patch.object(self.downloader, '_merge_video_audio', return_value=merged):
                result = self.downloader._merge_audio_into(video, audio)

        self.assertIsNone(result.content_hash)
        self.assertEqual(result.bytes_downloaded, 12)

    def test_gallery_urls_are_html_decoded(self):
        submission = SimpleNamespace(
            is_gallery=True,
            media_metadata={'item1': {'s': {'u': 'https://preview.redd.it/a.jpg?width=1&amp;s=abc'}}},
        )

        urls = RedditMediaDownloader.extract_media_urls_from_submission(submission)

        self.assertEqual(urls[0].url, 'https://preview.redd.it/a.jpg?width=1&s=abc')
        self.assertEqual(urls[0].gallery_id, 'item1')


if __name__ == '__main__':
    unittest.main()
