"""Regression tests for Reddit video and audio merging."""

import os
import tempfile
import unittest
from unittest.mock import patch

from utils.media_services.reddit_media import RedditMediaDownloader
from utils.service_abstractions import DownloadResult, DownloadStatus


class TestRedditVideoMerge(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.downloader = RedditMediaDownloader()
        self.video_url = 'https://v.redd.it/test123/DASH_720.mp4'
        self.audio_url = 'https://v.redd.it/test123/DASH_audio.mp4'
        self.save_path = os.path.join(self.temp_dir.name, 'video.mp4')

    def tearDown(self):
        self.temp_dir.cleanup()

    def _download(self, url, path):
        if url == self.video_url:
            with open(path, 'wb') as video_file:
                video_file.write(b'video')
        return DownloadResult(
            status=DownloadStatus.SUCCESS,
            local_path=path,
            bytes_downloaded=10 if url == self.video_url else 2,
        )

    def _download_video(self, merge):
        with patch.object(self.downloader, '_is_ffmpeg_available', return_value=True):
            with patch.object(self.downloader, '_get_audio_url_from_video_url', return_value=self.audio_url):
                with patch.object(self.downloader, 'download_file', side_effect=self._download):
                    with patch.object(self.downloader, '_merge_video_audio', side_effect=merge):
                        return self.downloader._download_reddit_video(self.video_url, self.save_path)

    def test_merge_uses_a_temporary_output(self):
        def merge(video_path, audio_path, output_path):
            self.assertEqual(video_path, self.save_path)
            self.assertNotEqual(output_path, video_path)
            with open(output_path, 'wb') as merged_file:
                merged_file.write(b'merged')
            return DownloadResult(status=DownloadStatus.SUCCESS, local_path=output_path)

        result = self._download_video(merge)

        self.assertTrue(result.is_success)
        self.assertEqual(result.local_path, self.save_path)
        self.assertEqual(result.bytes_downloaded, 12)
        with open(self.save_path, 'rb') as merged_file:
            self.assertEqual(merged_file.read(), b'merged')

    def test_merge_failure_keeps_the_downloaded_video(self):
        result = self._download_video(
            lambda *_: DownloadResult(status=DownloadStatus.FAILED, error_message='merge failed')
        )

        self.assertTrue(result.is_success)
        self.assertEqual(result.local_path, self.save_path)
        with open(self.save_path, 'rb') as video_file:
            self.assertEqual(video_file.read(), b'video')

    def test_promotion_failure_keeps_the_downloaded_video(self):
        def merge(_, __, output_path):
            with open(output_path, 'wb') as merged_file:
                merged_file.write(b'merged')
            return DownloadResult(status=DownloadStatus.SUCCESS, local_path=output_path)

        with patch('utils.media_services.reddit_media.os.replace', side_effect=OSError('disk full')):
            result = self._download_video(merge)

        self.assertTrue(result.is_success)
        self.assertEqual(result.local_path, self.save_path)
        with open(self.save_path, 'rb') as video_file:
            self.assertEqual(video_file.read(), b'video')


if __name__ == '__main__':
    unittest.main()
