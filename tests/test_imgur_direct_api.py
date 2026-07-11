"""Tests for the direct Imgur API path."""

import unittest
from unittest.mock import patch

from utils.media_services.imgur_media import ImgurMediaDownloader
from utils.service_abstractions import DownloadResult, DownloadStatus, ServiceConfig


class TestImgurDirectApi(unittest.TestCase):
    def test_configured_image_download_uses_direct_api(self):
        downloader = ImgurMediaDownloader(
            ServiceConfig(name='Imgur', api_keys={'client_ids': ['client-id']})
        )
        expected = DownloadResult(status=DownloadStatus.SUCCESS, local_path='/tmp/image.jpg')

        with patch.object(downloader, '_download_image_via_api', return_value=expected) as download:
            result = downloader._download_single_image('abc123', '/tmp/image.jpg', 'https://imgur.com/abc123')

        self.assertIs(result, expected)
        download.assert_called_once_with('abc123', '/tmp/image.jpg')
        self.assertFalse(hasattr(downloader, '_pyimgur_client'))


if __name__ == '__main__':
    unittest.main()
