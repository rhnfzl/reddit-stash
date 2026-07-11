"""Security tests for explicit HTTP redirect handling."""

import unittest
import socket
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from utils.media_services.base_downloader import BaseHTTPDownloader
from utils.service_abstractions import DownloadStatus, ServiceConfig


class TestRedirectSecurity(unittest.TestCase):
    def setUp(self):
        self.downloader = BaseHTTPDownloader(
            ServiceConfig(name='redirect-security', max_redirects=2)
        )

    def test_rejects_redirect_to_non_public_destination(self):
        redirect = MagicMock()
        redirect.status_code = 302
        redirect.headers = {'location': 'http://127.0.0.1/internal'}

        validator = MagicMock()
        validator.is_safe_for_download.side_effect = [True, False]
        validator.resolve_public_addresses.return_value = ('93.184.216.34',)
        with patch('utils.media_services.base_downloader.get_url_validator', return_value=validator):
            with patch.object(self.downloader._session, 'get', return_value=redirect) as get:
                result = self.downloader.download_file(
                    'https://public.example/image.jpg',
                    '/tmp/image.jpg',
                )

        self.assertEqual(result.status, DownloadStatus.INVALID_URL)
        self.assertEqual(get.call_count, 1)
        self.assertFalse(get.call_args.kwargs['allow_redirects'])

    def test_follows_validated_relative_redirect(self):
        redirect = MagicMock()
        redirect.status_code = 302
        redirect.headers = {'location': '/final.jpg'}
        final = MagicMock()
        final.status_code = 200
        final.headers = {'content-type': 'image/jpeg'}

        validator = MagicMock()
        validator.is_safe_for_download.side_effect = [True, True]
        validator.resolve_public_addresses.return_value = ('93.184.216.34',)
        with patch('utils.media_services.base_downloader.get_url_validator', return_value=validator):
            with patch.object(self.downloader._session, 'get', side_effect=[redirect, final]) as get:
                response, final_url = self.downloader._get_with_safe_redirects(
                    'https://public.example/image.jpg',
                    {'stream': True, 'allow_redirects': False},
                )

        self.assertIs(response, final)
        self.assertEqual(final_url, 'https://public.example/final.jpg')
        self.assertEqual(get.call_count, 2)
        self.assertTrue(all(not call.kwargs['allow_redirects'] for call in get.call_args_list))

    def test_cross_origin_redirect_removes_sensitive_headers(self):
        redirect = MagicMock()
        redirect.status_code = 302
        redirect.headers = {'location': 'https://cdn.example/final.jpg'}
        final = MagicMock()
        final.status_code = 200
        final.headers = {'content-type': 'image/jpeg'}

        validator = MagicMock()
        validator.is_safe_for_download.side_effect = [True, True]
        validator.resolve_public_addresses.return_value = ('93.184.216.34',)
        with patch('utils.media_services.base_downloader.get_url_validator', return_value=validator):
            with patch.object(self.downloader._session, 'get', side_effect=[redirect, final]) as get:
                self.downloader._get_with_safe_redirects(
                    'https://public.example/image.jpg',
                    {
                        'headers': {
                            'Authorization': 'Bearer secret',
                            'Cookie': 'session=secret',
                            'Proxy-Authorization': 'Basic secret',
                            'X-API-Key': 'secret',
                            'X-Request-ID': 'trace',
                        },
                    },
                )

        self.assertIn('Authorization', get.call_args_list[0].kwargs['headers'])
        self.assertEqual(get.call_args_list[1].kwargs['headers'], {'X-Request-ID': 'trace'})

    def test_same_origin_redirect_preserves_request_headers(self):
        redirect = MagicMock()
        redirect.status_code = 302
        redirect.headers = {'location': 'https://PUBLIC.EXAMPLE:443/final.jpg'}
        final = MagicMock()
        final.status_code = 200
        final.headers = {'content-type': 'image/jpeg'}

        validator = MagicMock()
        validator.is_safe_for_download.side_effect = [True, True]
        validator.resolve_public_addresses.return_value = ('93.184.216.34',)
        headers = {'Authorization': 'Bearer secret', 'X-Request-ID': 'trace'}
        with patch('utils.media_services.base_downloader.get_url_validator', return_value=validator):
            with patch.object(self.downloader._session, 'get', side_effect=[redirect, final]) as get:
                self.downloader._get_with_safe_redirects(
                    'https://public.example/image.jpg',
                    {'headers': headers},
                )

        self.assertEqual(get.call_args_list[1].kwargs['headers'], headers)

    def test_rejects_redirect_chain_beyond_configured_limit(self):
        redirects = []
        for index in range(3):
            response = MagicMock()
            response.status_code = 302
            response.headers = {'location': f'/hop-{index}.jpg'}
            redirects.append(response)

        validator = MagicMock()
        validator.is_safe_for_download.return_value = True
        validator.resolve_public_addresses.return_value = ('93.184.216.34',)
        with patch('utils.media_services.base_downloader.get_url_validator', return_value=validator):
            with patch.object(self.downloader._session, 'get', side_effect=redirects) as get:
                with self.assertRaisesRegex(ValueError, 'Redirect limit exceeded'):
                    self.downloader._get_with_safe_redirects(
                        'https://public.example/image.jpg',
                        {'stream': True, 'allow_redirects': False},
                    )

        self.assertEqual(get.call_count, 3)

    def test_standard_requests_preserves_hostname_while_pinning_dns(self):
        with self.downloader._pin_standard_requests(
            'public.example',
            ('93.184.216.34',),
        ):
            resolved = socket.getaddrinfo(
                'public.example',
                443,
                type=socket.SOCK_STREAM,
            )

        self.assertEqual(resolved[0][4], ('93.184.216.34', 443))

    def test_curl_requests_receive_pinned_resolve_option(self):
        curl_opt = SimpleNamespace(RESOLVE='RESOLVE')
        with patch('utils.media_services.base_downloader.CURL_CFFI_AVAILABLE', True):
            with patch('utils.media_services.base_downloader.CurlOpt', curl_opt):
                request_kwargs, _ = self.downloader._pin_resolved_addresses(
                    'https://public.example/image.jpg',
                    {'stream': True},
                    ('93.184.216.34',),
                )

        self.assertEqual(
            request_kwargs['curl_options']['RESOLVE'],
            ['public.example:443:93.184.216.34'],
        )


if __name__ == '__main__':
    unittest.main()
