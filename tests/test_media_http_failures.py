"""How HTTP, DNS and probe failures are classified by the downloaders."""

import subprocess
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

import requests

from tests.media_helpers import URL, failed_download, media_manager
from utils.media_services import base_downloader
from utils.media_services.base_downloader import BaseHTTPDownloader
from utils.media_services.imgur_media import ImgurMediaDownloader
from utils.media_services.reddit_media import RedditMediaDownloader
from utils.rate_limiter import RateLimitTimeout
from utils.media_services.media_types import DownloadStatus, ServiceConfig
from utils.media_services.url_security import URLSecurityValidator


class TestFailureClassification(unittest.TestCase):
    def setUp(self):
        self.manager = media_manager()

    def _fail_with(self, result):
        downloader = Mock()
        downloader.download.return_value = result
        self.manager._get_service_for_url = Mock(return_value=('reddit_image', downloader))
        with patch.object(self.manager, '_resolve_download_url', return_value=URL):
            self.manager._fetch(URL, '/x')

    def test_transient_failure_with_status_digits_in_its_url_is_not_permanent(self):
        self._fail_with(failed_download(message='Download timed out [URL: https://i.redd.it/a403b.jpg]'))

        self.assertNotIn(URL, self.manager._permanent_failures)
        self.assertEqual(self.manager._transient_failures[URL], 1)

    def test_typed_statuses_are_permanent(self):
        for status in (DownloadStatus.NOT_FOUND, DownloadStatus.FORBIDDEN, DownloadStatus.INVALID_URL):
            self.manager._permanent_failures.clear()
            self.manager._downloaded_urls.clear()
            self._fail_with(failed_download(status, 'whatever'))
            self.assertIn(URL, self.manager._permanent_failures)

    def test_imgur_error_text_is_not_read_as_a_status(self):
        downloader = ImgurMediaDownloader()
        downloader._client_ids = ['id']
        with patch.object(downloader, '_api_get', side_effect=ValueError('endpoint not found 404')):
            result = downloader._download_image_via_api('abc', '/x.jpg')
        self.assertEqual(result.status, DownloadStatus.FAILED)

        with patch.object(downloader, '_api_get', side_effect=RateLimitTimeout('waited')):
            result = downloader._download_image_via_api('abc', '/x.jpg')
        self.assertEqual(result.status, DownloadStatus.RATE_LIMITED)


class TestHttpErrorHandling(unittest.TestCase):
    """download_file's handlers must see what the retry layer gave up on."""

    def setUp(self):
        self.downloader = BaseHTTPDownloader(ServiceConfig(name='http-errors'))
        self.calls = 0

    def _download_raising(self, status_code, headers=None):
        def raise_http_error(*_args):
            self.calls += 1
            error = base_downloader.RequestsError('http error')
            error.response = SimpleNamespace(status_code=status_code, headers=headers or {})
            raise error

        with patch.object(self.downloader, '_download_with_retry', raise_http_error), \
                patch('tenacity.nap.time.sleep'), \
                patch.object(base_downloader.rate_limit_manager, 'report_response') as report:
            return self.downloader.download_file(URL, '/tmp/x.jpg'), report

    def test_429_is_rate_limited_and_reports_retry_after(self):
        result, report = self._download_raising(429, {'retry-after': '5'})

        self.assertEqual(result.status, DownloadStatus.RATE_LIMITED)
        self.assertEqual(result.retry_after, 5)
        self.assertEqual(self.calls, 1)
        report.assert_called_once_with('http-errors', 429, 5)

    def test_429_without_a_header_leaves_the_wait_to_the_service_policy(self):
        result, report = self._download_raising(429)

        self.assertIsNone(result.retry_after)
        report.assert_called_once_with('http-errors', 429, None)

    def test_generic_429_does_not_stall_other_hosts(self):
        self.downloader = BaseHTTPDownloader(ServiceConfig(name='Generic'))

        result, report = self._download_raising(429, {'retry-after': '120'})

        self.assertEqual(result.status, DownloadStatus.RATE_LIMITED)
        self.assertEqual(result.retry_after, 120)
        report.assert_not_called()

    def test_429_with_a_date_retry_after_still_returns_a_result(self):
        result, _ = self._download_raising(429, {'retry-after': 'Wed, 21 Oct 2026 07:28:00 GMT'})

        self.assertEqual(result.status, DownloadStatus.RATE_LIMITED)
        self.assertIsNone(result.retry_after)

    def test_404_and_403_are_typed_and_not_retried(self):
        for code, status in ((404, DownloadStatus.NOT_FOUND), (403, DownloadStatus.FORBIDDEN)):
            self.calls = 0
            result, _ = self._download_raising(code)
            self.assertEqual(result.status, status)
            self.assertEqual(self.calls, 1)

    def test_server_error_is_retried_then_reported(self):
        result, report = self._download_raising(503)

        self.assertEqual(result.status, DownloadStatus.FAILED)
        self.assertEqual(self.calls, 3)
        report.assert_called_once_with('http-errors', 503)

    def test_transport_error_without_a_response_is_retried(self):
        def raise_transport_error(*_args):
            self.calls += 1
            raise base_downloader.RequestsError('stream reset')

        with patch.object(self.downloader, '_download_with_retry', raise_transport_error), \
                patch('tenacity.nap.time.sleep'):
            result = self.downloader.download_file(URL, '/tmp/x.jpg')

        self.assertEqual(result.status, DownloadStatus.FAILED)
        self.assertEqual(self.calls, 3)

    def test_rate_limit_wait_timeout_is_rate_limited(self):
        with patch.object(self.downloader, '_respect_rate_limit', side_effect=RateLimitTimeout('waited')):
            result = self.downloader.download_file(URL, '/tmp/x.jpg')

        self.assertEqual(result.status, DownloadStatus.RATE_LIMITED)

    def test_requests_fallback_leaves_retries_to_the_download_policy(self):
        with patch.multiple(
            base_downloader, CURL_CFFI_AVAILABLE=False, requests=requests, HTTPAdapter=requests.adapters.HTTPAdapter,
        ):
            downloader = BaseHTTPDownloader(ServiceConfig(name='http-errors'))

        self.assertEqual(downloader._session.get_adapter('https://example.com').max_retries.total, 0)

    def test_dash_fallback_ignores_404_digits_in_the_url(self):
        downloader = RedditMediaDownloader()
        with patch.object(downloader, 'download_file', return_value=failed_download(message='timed out DASH_404')) as get:
            result = downloader._try_dash_qualities('https://v.redd.it/a/DASH_1080.mp4', '/x.mp4')

        self.assertEqual(result.status, DownloadStatus.FAILED)
        self.assertEqual(get.call_count, 1)


class TestDnsFailureClassification(unittest.TestCase):
    def test_dns_failure_does_not_excuse_a_real_rejection(self):
        validator = URLSecurityValidator()
        for url in ('https://user:pw@dead.example.com/a.jpg',
                    'ftp://dead.example.com/a.jpg',
                    'https://dead.example.com/../../etc/passwd'):
            with self.subTest(url=url), \
                    patch('utils.media_services.url_security._resolve_hostname', side_effect=OSError('no dns')):
                result = validator.validate_url(url)
                self.assertFalse(result.is_valid)
                self.assertFalse(result.dns_failed)

    def test_trusted_host_dns_failure_is_transient_not_invalid(self):
        downloader = RedditMediaDownloader()
        with patch('utils.media_services.url_security._resolve_hostname', side_effect=OSError('no dns')), \
                patch('time.sleep'), \
                patch.object(downloader._session, 'get') as get:
            result = downloader.download_file('https://i.redd.it/a.jpg', '/tmp/a.jpg')
        get.assert_not_called()
        self.assertEqual(result.status, DownloadStatus.FAILED)

    def test_redirect_to_a_private_address_stays_invalid(self):
        downloader = BaseHTTPDownloader(ServiceConfig(name='t'))
        with patch('utils.media_services.url_security._resolve_hostname',
                   return_value=[(None, None, None, None, ('127.0.0.1', 0))]):
            result = downloader.download_file('https://internal.example.com/a.jpg', '/tmp/a.jpg')
        self.assertEqual(result.status, DownloadStatus.INVALID_URL)


class TestProbes(unittest.TestCase):
    def test_ffmpeg_probe_that_hangs_counts_as_unavailable(self):
        downloader = RedditMediaDownloader()
        with patch(
            'utils.media_services.reddit_media.subprocess.run',
            side_effect=subprocess.TimeoutExpired('ffmpeg', 10),
        ) as run:
            self.assertFalse(downloader._is_ffmpeg_available())
        self.assertIn('timeout', run.call_args.kwargs)

    def test_audio_probe_refuses_redirect_to_a_private_address(self):
        downloader = RedditMediaDownloader()
        redirect = MagicMock(status_code=302, headers={'location': 'http://127.0.0.1/internal'})
        validator = MagicMock()
        validator.is_safe_for_download.side_effect = lambda url: url.startswith('https://v.redd.it')
        validator.validate_url.return_value.dns_failed = False
        validator.resolve_public_addresses.return_value = ('93.184.216.34',)

        with patch('utils.media_services.safe_http.get_url_validator', return_value=validator), \
                patch.object(downloader._session, 'head', return_value=redirect) as head:
            audio = downloader._get_audio_url_from_video_url('https://v.redd.it/a/DASH_720.mp4')

        self.assertIsNone(audio)
        self.assertFalse(head.call_args.kwargs['allow_redirects'])


class TestProbeRedirectPolicy(unittest.TestCase):
    def test_imgur_direct_probe_does_not_follow_redirects(self):
        downloader = ImgurMediaDownloader()
        with patch.object(downloader._session, 'head', return_value=MagicMock(status_code=200)) as head, \
                patch.object(downloader, 'download_file', return_value=failed_download()):
            downloader._download_image_direct('abc', '/tmp/abc.jpg')
        self.assertFalse(head.call_args.kwargs['allow_redirects'])


if __name__ == '__main__':
    unittest.main()
