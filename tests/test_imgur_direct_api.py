"""Tests for the direct Imgur API path."""

import unittest
from unittest.mock import Mock, patch

from utils.media_services.imgur_media import ImgurMediaDownloader
from utils.service_abstractions import (
    DownloadResult,
    DownloadStatus,
    MediaMetadata,
    MediaType,
    ServiceConfig,
)


class TestImgurDirectApi(unittest.TestCase):
    def test_api_get_rotates_client_id_after_rate_limit(self):
        downloader = ImgurMediaDownloader(
            ServiceConfig(name='Imgur', api_keys={'client_ids': ['first', 'second']})
        )
        rate_limited = Mock(status_code=429)
        success = Mock(status_code=200)
        with patch.object(downloader._session, 'get', side_effect=[rate_limited, success]) as api_get:
            response = downloader._api_get('https://api.imgur.com/3/image/abc123', (5.0, 10.0))

        self.assertIs(response, success)
        self.assertEqual(api_get.call_count, 2)
        self.assertEqual(
            api_get.call_args_list[0].kwargs['headers']['Authorization'],
            'Client-ID first',
        )
        self.assertEqual(
            api_get.call_args_list[1].kwargs['headers']['Authorization'],
            'Client-ID second',
        )

    def test_api_get_reports_final_rate_limit_after_all_client_ids_are_exhausted(self):
        downloader = ImgurMediaDownloader(
            ServiceConfig(name='Imgur', api_keys={'client_ids': ['first', 'second']})
        )
        first_limited = Mock(status_code=429, headers={'Retry-After': '15'})
        final_limited = Mock(status_code=429, headers={'Retry-After': '45'})

        with patch.object(downloader, '_respect_rate_limit'):
            with patch.object(
                downloader._session,
                'get',
                side_effect=[first_limited, final_limited],
            ):
                with patch('utils.media_services.imgur_media.rate_limit_manager.report_response') as report:
                    response = downloader._api_get(
                        'https://api.imgur.com/3/image/abc123',
                        (5.0, 10.0),
                    )

        self.assertIsNone(response)
        first_limited.close.assert_called_once_with()
        final_limited.close.assert_called_once_with()
        report.assert_called_once_with('imgur', 429, 45)

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

    def test_api_download_preserves_content_hash(self):
        downloader = ImgurMediaDownloader(
            ServiceConfig(name='Imgur', api_keys={'client_ids': ['client-id']})
        )
        api_response = Mock(status_code=200)
        api_response.json.return_value = {
            'success': True,
            'data': {'link': 'https://i.imgur.com/abc123.jpg'},
        }
        downloaded = DownloadResult(
            status=DownloadStatus.SUCCESS,
            local_path='/tmp/image.jpg',
            content_hash='content-checksum',
            metadata=MediaMetadata(
                url='https://i.imgur.com/abc123.jpg',
                media_type=MediaType.IMAGE,
            ),
        )

        with patch.object(downloader, '_api_get', return_value=api_response):
            with patch.object(downloader, 'download_file', return_value=downloaded):
                result = downloader._download_image_via_api('abc123', '/tmp/image.jpg')

        self.assertEqual(result.content_hash, 'content-checksum')

    def test_gallery_download_uses_gallery_endpoint(self):
        downloader = ImgurMediaDownloader(
            ServiceConfig(name='Imgur', api_keys={'client_ids': ['client-id']})
        )
        api_response = Mock(status_code=200)
        api_response.json.return_value = {'success': True, 'data': {'images': []}}

        with patch.object(downloader, '_api_get', return_value=api_response) as api_get:
            downloader._download_gallery('gallery-id', '/tmp/gallery.jpg')

        api_get.assert_called_once_with(
            'https://api.imgur.com/3/gallery/gallery-id',
            (5.0, 15.0),
        )


if __name__ == '__main__':
    unittest.main()
