"""Tests for the direct Imgur API path."""

import os
import unittest
from unittest.mock import Mock, patch

from requests.structures import CaseInsensitiveDict

from tests.media_helpers import media_manager
from utils.media_services.imgur_media import ImgurMediaDownloader
from utils.rate_limiter import RateLimitTimeout
from utils.media_services.media_types import (
    DownloadResult,
    DownloadStatus,
    MediaMetadata,
    MediaType,
    ServiceConfig,
)


def _downloader(*client_ids):
    downloader = ImgurMediaDownloader(ServiceConfig(name='Imgur'))
    downloader.set_client_credentials(list(client_ids))
    return downloader


class TestImgurEnvironmentCredentials(unittest.TestCase):
    def _configured_ids(self, env):
        manager = media_manager()
        with patch.dict(os.environ):
            os.environ.pop('IMGUR_CLIENT_IDS', None)
            os.environ.pop('IMGUR_CLIENT_ID', None)
            os.environ.update(env)
            manager._initialize_services()
        return manager._imgur_downloader._client_ids

    def test_comma_separated_ids_reach_the_downloader(self):
        self.assertEqual(self._configured_ids({'IMGUR_CLIENT_IDS': 'first, second'}), ['first', 'second'])

    def test_single_id_reaches_the_downloader(self):
        self.assertEqual(self._configured_ids({'IMGUR_CLIENT_ID': 'only'}), ['only'])


class TestImgurDirectApi(unittest.TestCase):
    def test_api_get_rotates_client_id_after_rate_limit(self):
        downloader = _downloader('first', 'second')
        rate_limited = Mock(status_code=429)
        success = Mock(status_code=200)
        with patch.object(downloader, '_respect_rate_limit'), \
                patch.object(downloader._session, 'get', side_effect=[rate_limited, success]) as api_get:
            response, _ = downloader._api_get('https://api.imgur.com/3/image/abc123', (5.0, 10.0))

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
        downloader = _downloader('first', 'second')
        first_limited = Mock(status_code=429, headers=CaseInsensitiveDict({'Retry-After': '15'}))
        final_limited = Mock(status_code=429, headers=CaseInsensitiveDict({'Retry-After': '45'}))

        with patch.object(downloader, '_respect_rate_limit'):
            with patch.object(
                downloader._session,
                'get',
                side_effect=[first_limited, final_limited],
            ):
                with patch('utils.media_services.imgur_media.rate_limit_manager.report_response') as report:
                    response, retry_after = downloader._api_get(
                        'https://api.imgur.com/3/image/abc123',
                        (5.0, 10.0),
                    )

        self.assertIsNone(response)
        self.assertEqual(retry_after, 45)
        first_limited.close.assert_called_once_with()
        final_limited.close.assert_called_once_with()
        report.assert_called_once_with('imgur', 429, 45)

    def test_api_get_reports_a_response_that_is_not_rate_limited(self):
        downloader = _downloader('first')
        success = Mock(status_code=200)

        with patch.object(downloader, '_respect_rate_limit'), \
                patch.object(downloader._session, 'get', return_value=success), \
                patch('utils.media_services.imgur_media.rate_limit_manager.report_response') as report:
            downloader._api_get('https://api.imgur.com/3/image/abc123', (5.0, 10.0))

        report.assert_called_once_with('imgur', 200)

    def test_head_probe_reports_and_closes_and_stops_at_a_rate_limit(self):
        downloader = ImgurMediaDownloader(ServiceConfig(name='Imgur'))
        missing = Mock(status_code=404, headers={})
        limited = Mock(status_code=429, headers=CaseInsensitiveDict({'Retry-After': '45'}))

        with patch.object(downloader, '_respect_rate_limit'), \
                patch.object(downloader._session, 'head', side_effect=[missing, limited]) as head, \
                patch.object(downloader, 'download_file') as download, \
                patch('utils.media_services.imgur_media.rate_limit_manager.report_response') as report:
            result = downloader._download_image_direct('abc123', '/tmp/image.jpg')

        self.assertEqual(result.status, DownloadStatus.RATE_LIMITED)
        download.assert_not_called()
        self.assertEqual(head.call_count, 2)
        missing.close.assert_called_once_with()
        limited.close.assert_called_once_with()
        report.assert_called_with('imgur', 429, 45)

    def test_head_probe_stops_when_the_rate_limit_wait_times_out(self):
        downloader = ImgurMediaDownloader(ServiceConfig(name='Imgur'))

        with patch.object(downloader, '_respect_rate_limit', side_effect=RateLimitTimeout('waited')) as wait, \
                patch.object(downloader, 'download_file') as download:
            result = downloader._download_image_direct('abc123', '/tmp/image.jpg')

        self.assertEqual(result.status, DownloadStatus.RATE_LIMITED)
        wait.assert_called_once_with()
        download.assert_not_called()

    def test_api_download_preserves_final_retry_after_value(self):
        downloader = _downloader('client-id')
        rate_limited = Mock(status_code=429, headers=CaseInsensitiveDict({'Retry-After': '45'}))

        with patch.object(downloader, '_respect_rate_limit'):
            with patch.object(downloader._session, 'get', return_value=rate_limited):
                with patch('utils.media_services.imgur_media.rate_limit_manager.report_response'):
                    result = downloader._download_image_via_api('abc123', '/tmp/image.jpg')

        self.assertEqual(result.status, DownloadStatus.RATE_LIMITED)
        self.assertEqual(result.retry_after, 45)

    def test_configured_image_download_uses_direct_api(self):
        downloader = _downloader('client-id')
        expected = DownloadResult(status=DownloadStatus.SUCCESS, local_path='/tmp/image.jpg')

        with patch.object(downloader, '_download_image_via_api', return_value=expected) as download:
            result = downloader._download_single_image('abc123', '/tmp/image.jpg')

        self.assertIs(result, expected)
        download.assert_called_once_with('abc123', '/tmp/image.jpg')
        self.assertFalse(hasattr(downloader, '_pyimgur_client'))

    def test_api_download_preserves_content_hash(self):
        downloader = _downloader('client-id')
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

        with patch.object(downloader, '_api_get', return_value=(api_response, None)):
            with patch.object(downloader, 'download_file', return_value=downloaded):
                result = downloader._download_image_via_api('abc123', '/tmp/image.jpg')

        self.assertEqual(result.content_hash, 'content-checksum')

    def test_gallery_download_uses_gallery_endpoint(self):
        downloader = _downloader('client-id')
        api_response = Mock(status_code=200)
        api_response.json.return_value = {'success': True, 'data': {'images': []}}

        with patch.object(downloader, '_api_get', return_value=(api_response, None)) as api_get:
            downloader.download('https://imgur.com/gallery/gallery', '/tmp/gallery.jpg')

        api_get.assert_called_once_with(
            'https://api.imgur.com/3/gallery/gallery',
            (5.0, 15.0),
        )


if __name__ == '__main__':
    unittest.main()
