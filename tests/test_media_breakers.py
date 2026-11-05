"""Which download outcomes count against a service's circuit breaker."""

import itertools
import os
import tempfile
import unittest
from unittest.mock import MagicMock, Mock, patch

import requests

from tests.media_helpers import URL, failed_download, media_manager
from utils.media_services.error_isolation import CircuitBreaker, CircuitBreakerConfig, CircuitState
from utils.media_services.base_downloader import BaseHTTPDownloader
from utils.media_services.imgur_media import ImgurMediaDownloader
from utils.media_services.media_types import DownloadResult, DownloadStatus, SERVICE_FAILURE_STATUSES, ServiceConfig


class TestCircuitBreaker(unittest.TestCase):
    def setUp(self):
        self.breaker = CircuitBreaker('svc', CircuitBreakerConfig(failure_threshold=2))

    def _call(self, result):
        outcome = self.breaker.call(lambda: result, is_failure=lambda r: r.status == DownloadStatus.FAILED)
        return 'fallback' if outcome is None else outcome

    def test_failed_results_open_the_breaker(self):
        failed = failed_download()
        self.assertIs(self._call(failed), failed)  # The typed result survives.
        self._call(failed)

        self.assertTrue(self.breaker.is_rejecting())
        self.assertEqual(self._call(failed), 'fallback')

    def test_item_problems_do_not_open_the_breaker(self):
        for _ in range(5):
            self._call(failed_download(DownloadStatus.NOT_FOUND))

        self.assertFalse(self.breaker.is_rejecting())

    def test_slow_completed_call_keeps_its_result(self):
        result = DownloadResult(status=DownloadStatus.SUCCESS, local_path='/x')
        with patch('utils.media_services.error_isolation.time.time', side_effect=itertools.count(step=1000)):
            self.assertIs(self._call(result), result)

    def test_failure_while_half_open_reopens_at_once(self):
        breaker = self.breaker
        breaker.stats.state = CircuitState.HALF_OPEN
        breaker.stats.failure_count = 0

        self._call(failed_download())

        self.assertEqual(breaker.stats.state, CircuitState.OPEN)

    def test_generic_service_is_not_tripped_by_failed_results(self):
        manager = media_manager()
        downloader = Mock()
        downloader.download.return_value = failed_download()

        for _ in range(10):
            manager._download_protected('generic', downloader, URL, '/x')

        self.assertFalse(manager._breaker('generic').is_rejecting())

    def test_rate_limited_results_open_a_service_breaker(self):
        manager = media_manager()
        downloader = Mock()
        downloader.download.return_value = failed_download(DownloadStatus.RATE_LIMITED)

        for _ in range(5):
            manager._download_protected('imgur', downloader, URL, '/x')

        self.assertTrue(manager._breaker('imgur').is_rejecting())
        self.assertFalse(manager._breaker('generic').is_rejecting())

    def test_raising_download_gives_a_failed_result(self):
        manager = media_manager()
        downloader = Mock()
        downloader.download.side_effect = RuntimeError('service down')

        result = manager._download_protected('imgur', downloader, URL, '/x')

        self.assertEqual(result.status, DownloadStatus.FAILED)
        self.assertEqual(result.error_message, 'Service unavailable')

    def test_rejected_call_keeps_a_live_url_out_of_recovery(self):
        manager = media_manager()
        manager._recovery_service.is_enabled.return_value = True
        downloader = Mock()
        downloader.download.return_value = failed_download()
        for _ in range(5):
            manager._download_protected('imgur', downloader, URL, '/x')
        downloader.reset_mock()
        manager._get_service_for_url = Mock(return_value=('imgur', downloader))

        with patch.object(manager, '_resolve_download_url', return_value=URL):
            self.assertIsNone(manager.download_media(URL, '/x').local_path)

        downloader.download.assert_not_called()
        manager._recovery_service.attempt_recovery.assert_not_called()
        self.assertEqual(manager._transient_failures, {})
        self.assertEqual(manager._retry_queue.add_failed_download.call_args.args[2], 'imgur')


class TestPerItemFailuresSpareTheBreaker(unittest.TestCase):
    def _dead_album_result(self, response):
        downloader = ImgurMediaDownloader()
        downloader._client_ids = ['id']
        with patch.object(downloader, '_api_get', return_value=(response, None)):
            return downloader._download_album_direct('dead', '/tmp/dead.jpg')

    def test_dead_album_is_not_found(self):
        response = MagicMock(status_code=404)
        response.raise_for_status.side_effect = requests.HTTPError('404 Not Found')
        self.assertEqual(self._dead_album_result(response).status, DownloadStatus.NOT_FOUND)

    def test_album_without_client_ids_is_rejected_not_a_service_failure(self):
        downloader = ImgurMediaDownloader()
        downloader._client_ids = []

        result = downloader._download_album_direct('abc', '/tmp/abc.jpg')

        self.assertEqual(result.status, DownloadStatus.REJECTED)
        self.assertNotIn(result.status, SERVICE_FAILURE_STATUSES)

    def test_album_without_data_is_not_found(self):
        response = MagicMock(status_code=200)
        response.json.return_value = {'success': False}
        self.assertEqual(self._dead_album_result(response).status, DownloadStatus.NOT_FOUND)

    def _album_status(self, image_results):
        downloader = ImgurMediaDownloader()
        downloader._client_ids = ['id']
        response = MagicMock(status_code=200)
        response.json.return_value = {'success': True, 'data': {
            'images': [{'link': f'https://i.imgur.com/{i}.jpg'} for i in range(len(image_results))],
        }}
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(downloader, '_api_get', return_value=(response, None)), \
                patch.object(downloader, 'download_file', side_effect=image_results):
            return downloader.download('https://imgur.com/a/abc', os.path.join(folder, 'a.jpg')).status

    def test_album_status_follows_what_its_images_say(self):
        gone, forbidden = failed_download(DownloadStatus.NOT_FOUND), failed_download(DownloadStatus.FORBIDDEN)
        cases = (
            ([], DownloadStatus.NOT_FOUND),
            ([gone, gone], DownloadStatus.NOT_FOUND),
            ([gone, forbidden], DownloadStatus.REJECTED),
            ([gone, failed_download(DownloadStatus.RATE_LIMITED)], DownloadStatus.FAILED),
            ([gone, failed_download()], DownloadStatus.FAILED),
            ([gone, OSError('boom')], DownloadStatus.FAILED),
            ([DownloadResult(status=DownloadStatus.SUCCESS, local_path='/a.jpg'), OSError('boom')],
             DownloadStatus.FAILED),
        )
        for image_results, expected in cases:
            with self.subTest(expected=expected, image_results=image_results):
                self.assertEqual(self._album_status(image_results), expected)

    def test_unusable_files_are_rejected_not_failed(self):
        downloader = BaseHTTPDownloader(
            ServiceConfig(name='t', max_file_size=10, allowed_content_types=['image/*'])
        )
        for headers in ({'content-type': 'application/x-evil'},
                        {'content-type': 'image/jpeg', 'content-length': '11'},
                        {'content-type': 'image/jpeg'}):
            response = MagicMock(status_code=200, headers=headers)
            response.__enter__.return_value = response
            response.iter_content.return_value = [b'x' * 11]
            with self.subTest(headers=headers), tempfile.TemporaryDirectory() as d, \
                    patch.object(downloader, '_get_with_safe_redirects',
                                 return_value=(response, 'https://example.com/a.jpg')):
                result = downloader._download_with_retry('https://example.com/a.jpg', os.path.join(d, 'a.jpg'))
                self.assertEqual(result.status, DownloadStatus.REJECTED)

    def test_html_page_is_rejected_not_failed(self):
        response = MagicMock(status_code=200, headers={'content-type': 'text/html'})
        response.__enter__.return_value = response
        response.iter_content.return_value = [b'<html>' * 1000]
        downloader = BaseHTTPDownloader(ServiceConfig(name='t'))
        with tempfile.TemporaryDirectory() as d, \
                patch.object(downloader, '_get_with_safe_redirects',
                             return_value=(response, 'https://example.com/a.jpg')):
            result = downloader._download_with_retry('https://example.com/a.jpg', os.path.join(d, 'a.jpg'))
        self.assertEqual(result.status, DownloadStatus.REJECTED)

    def test_html_page_does_not_overwrite_an_existing_file(self):
        response = MagicMock(status_code=200, headers={'content-type': 'text/html'})
        response.__enter__.return_value = response
        response.iter_content.return_value = [b'<html>' * 1000]
        downloader = BaseHTTPDownloader(ServiceConfig(name='t'))
        with tempfile.TemporaryDirectory() as d, \
                patch.object(downloader, '_get_with_safe_redirects',
                             return_value=(response, 'https://example.com/a.jpg')):
            path = os.path.join(d, 'a.jpg')
            with open(path, 'wb') as existing:
                existing.write(b'backed up earlier')
            result = downloader._download_with_retry('https://example.com/a.jpg', path)
            with open(path, 'rb') as existing:
                self.assertEqual(existing.read(), b'backed up earlier')
        self.assertEqual(result.status, DownloadStatus.REJECTED)

    def test_rejected_does_not_trip_the_breaker_and_stays_transient(self):
        manager = media_manager()
        downloader = Mock()
        downloader.download.return_value = failed_download(DownloadStatus.REJECTED)
        for _ in range(6):
            result = manager._download_protected('imgur', downloader, URL, '/x.jpg')
        self.assertEqual(result.status, DownloadStatus.REJECTED)
        self.assertEqual(downloader.download.call_count, 6)

        with manager._url_lock:
            manager._record_failure(URL, result.status)
        self.assertNotIn(URL, manager._permanent_failures)
        self.assertEqual(manager._transient_failures[URL], 1)


if __name__ == '__main__':
    unittest.main()
