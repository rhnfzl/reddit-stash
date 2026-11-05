"""A provider that got no answer must not be remembered as an archive miss."""

import os
import sqlite3
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch


from utils.content_recovery.arctic_shift import ArcticShiftClient
from utils.content_recovery.recovery_metadata import ArchiveLookupError
from utils.content_recovery.cache_manager import RecoveryCacheManager
from utils.content_recovery.providers.arctic_shift_provider import ArcticShiftProvider
from utils.content_recovery.providers.pullpush_provider import PullPushProvider
from utils.content_recovery.providers.wayback_provider import WaybackMachineProvider
from utils.content_recovery.recovery_metadata import RecoveryResult, RecoverySource
from utils.gdpr_processor import _fetch_from_archive
from utils.media_services.download_manager import download_media_into
from utils.media_services.media_types import DownloadResult, DownloadStatus
from utils.media_services.reddit_media import RedditMediaDownloader
from utils.save_utils import _get_video_download_url, _is_video_url
from tests.recovery_helpers import RATE_LIMIT_TOKEN, WAYBACK, fake_response, fake_session, recovery_service
from tests.run_recovery_tests import PROVIDER_MODULES

REDDIT_URL = 'https://www.reddit.com/r/python/comments/abc123/example/'
PULLPUSH = 'utils.content_recovery.providers.pullpush_provider.rate_limit_manager'
ARCTIC = 'utils.content_recovery.arctic_shift.rate_limit_manager'


class TestRecoveryRunnerSelection(unittest.TestCase):
    def test_provider_runs_include_these_failure_cases(self):
        for provider in ('wayback', 'pullpush'):
            with self.subTest(provider=provider):
                self.assertIn('tests.test_recovery_provider_failures', PROVIDER_MODULES[provider])


class TestProviderFailuresAreNotArchiveMisses(unittest.TestCase):
    def test_arctic_shift_server_error_is_not_a_miss(self):
        provider = ArcticShiftProvider(
            client=ArcticShiftClient(session=fake_session(fake_response(503, reason='Service Unavailable'))),
        )
        with patch(ARCTIC):
            result = provider.attempt_recovery(REDDIT_URL)

        self.assertFalse(result.success)
        self.assertFalse(result.confirmed_miss)

    def test_arctic_shift_rate_limit_deferral_is_not_a_miss(self):
        provider = ArcticShiftProvider(client=ArcticShiftClient(session=fake_session()))
        with patch(ARCTIC) as manager:
            manager.acquire.return_value = False
            result = provider.attempt_recovery(REDDIT_URL)

        self.assertFalse(result.success)
        self.assertFalse(result.confirmed_miss)

    def test_arctic_shift_empty_answer_is_a_miss(self):
        provider = ArcticShiftProvider(
            client=ArcticShiftClient(session=fake_session(fake_response(payload={'data': []}))),
        )
        with patch(ARCTIC):
            result = provider.attempt_recovery(REDDIT_URL)

        self.assertTrue(result.confirmed_miss)

    def test_pullpush_server_error_is_not_a_miss(self):
        provider = PullPushProvider()
        provider.session = fake_session(fake_response(503, reason='Service Unavailable'))
        with patch(PULLPUSH):
            result = provider.attempt_recovery(REDDIT_URL)

        self.assertFalse(result.success)
        self.assertFalse(result.confirmed_miss)

    def test_pullpush_http_404_text_is_not_a_miss(self):
        provider = PullPushProvider()
        provider.session = fake_session(fake_response(404, reason='Not Found'))
        with patch(PULLPUSH):
            result = provider.attempt_recovery(REDDIT_URL)

        self.assertIn('not found', result.error_message.lower())
        self.assertFalse(result.confirmed_miss)

    def test_pullpush_empty_answer_is_a_miss(self):
        provider = PullPushProvider()
        provider.session = fake_session(fake_response(payload={'data': []}))
        with patch(PULLPUSH):
            result = provider.attempt_recovery(REDDIT_URL)

        self.assertTrue(result.confirmed_miss)

    def test_unusable_success_bodies_are_not_misses(self):
        comment_url = 'https://www.reddit.com/r/python/comments/abc123/example/def456/'
        for url in (REDDIT_URL, comment_url):
            for payload in ({'error': 'overloaded'}, {'data': 'x'}, [], None):
                with self.subTest(url=url, payload=payload):
                    provider = PullPushProvider()
                    provider.session = fake_session(fake_response(payload=payload))
                    with patch(PULLPUSH):
                        result = provider.attempt_recovery(url)

                    self.assertFalse(result.success)
                    self.assertFalse(result.confirmed_miss)

    def test_pullpush_comment_bad_json_is_not_a_miss(self):
        def bad_json():
            raise ValueError('not JSON')

        response = fake_response()
        response.json = bad_json
        provider = PullPushProvider()
        provider.session = fake_session(response)
        comment_url = 'https://www.reddit.com/r/python/comments/abc123/example/def456/'
        with patch(PULLPUSH):
            result = provider.attempt_recovery(comment_url)

        self.assertFalse(result.confirmed_miss)

    def test_arctic_shift_unusable_body_is_not_a_miss(self):
        provider = ArcticShiftProvider(
            client=ArcticShiftClient(session=fake_session(fake_response(payload={'error': 'overloaded'}))),
        )
        with patch(ARCTIC):
            result = provider.attempt_recovery(REDDIT_URL)

        self.assertFalse(result.confirmed_miss)

    def _wayback(self, *responses):
        provider = WaybackMachineProvider()
        provider.session = fake_session(*responses)
        with patch(WAYBACK), patch(RATE_LIMIT_TOKEN, return_value=True):
            return provider.attempt_recovery('https://example.com/a.jpg')

    def test_wayback_unusable_bodies_are_not_misses(self):
        empty = {'archived_snapshots': {}}
        for availability, cdx in (({'error': 'x'}, []), (empty, {'error': 'x'})):
            with self.subTest(availability=availability, cdx=cdx):
                result = self._wayback(fake_response(payload=availability), fake_response(payload=cdx))

                self.assertFalse(result.confirmed_miss)
        self.assertTrue(self._wayback(fake_response(payload=empty), fake_response(payload=[])).confirmed_miss)

    def test_wayback_unusable_capture_rows_are_not_misses(self):
        empty = {'archived_snapshots': {}}
        for cdx in ([['h'], ['x']], [['h'], 'x'], [['h'], 5]):
            with self.subTest(cdx=cdx):
                self.assertFalse(self._wayback(fake_response(payload=empty), fake_response(payload=cdx)).confirmed_miss)
        self.assertTrue(self._wayback(fake_response(payload=empty), fake_response(payload=[['h']])).confirmed_miss)

    def test_wayback_cdx_asks_for_the_most_recent_capture(self):
        provider = WaybackMachineProvider()
        provider.session = fake_session(fake_response(payload=[]))
        with patch(WAYBACK), patch(RATE_LIMIT_TOKEN, return_value=True):
            provider._search_cdx('https://example.com/a.jpg')

        params = provider.session.get.call_args.kwargs['params']
        self.assertEqual((params['sort'], params['reverse']), ('timestamp', 'true'))

    def test_arctic_shift_unusable_records_are_not_misses(self):
        for row in ('x', {'title': 'no id'}):
            with self.subTest(row=row):
                provider = ArcticShiftProvider(
                    client=ArcticShiftClient(session=fake_session(fake_response(payload={'data': [row]}))),
                )
                with patch(ARCTIC):
                    result = provider.attempt_recovery(REDDIT_URL)

                self.assertFalse(result.confirmed_miss)

    def test_arctic_shift_keeps_good_records_beside_an_unusable_one(self):
        client = ArcticShiftClient(session=fake_session(fake_response(payload={'data': ['x', {'id': 'abc123'}]})))
        with patch(ARCTIC), self.assertRaises(ArchiveLookupError) as caught:
            client.fetch_posts(['abc123', 'def456'])

        self.assertEqual(list(caught.exception.records), ['abc123'])

    def test_wayback_miss_needs_both_apis_to_answer(self):
        empty = {'archived_snapshots': {}}
        failure = fake_response(503, reason='Service Unavailable')

        self.assertFalse(self._wayback(failure, fake_response(payload=[])).confirmed_miss)
        self.assertFalse(self._wayback(fake_response(payload=empty), failure).confirmed_miss)
        self.assertTrue(self._wayback(fake_response(payload=empty), fake_response(payload=[])).confirmed_miss)

    def test_wayback_cdx_still_recovers_after_availability_failure(self):
        result = self._wayback(
            fake_response(503, reason='Service Unavailable'),
            fake_response(payload=[['urlkey', 'timestamp', 'original'], ['k', '20200101000000', 'https://example.com/a.jpg']]),
        )

        self.assertTrue(result.success)
        self.assertEqual(result.recovered_url, 'https://web.archive.org/web/20200101000000/https://example.com/a.jpg')


class TestRateLimitWaitStaysAResult(unittest.TestCase):
    """A rate limit wait that runs out is a failure result, recorded like any other."""

    def _assert_deferred(self, provider, target):
        with patch(target) as manager:
            manager.acquire.return_value = False
            result = provider.attempt_recovery(REDDIT_URL)

        self.assertFalse(result.success)
        self.assertFalse(result.confirmed_miss)
        self.assertIn('deferred by rate limiter', result.error_message)
        return result

    def test_pullpush_returns_a_failure_instead_of_raising(self):
        self._assert_deferred(PullPushProvider(), PULLPUSH)

    def test_wayback_returns_a_failure_instead_of_raising(self):
        self._assert_deferred(WaybackMachineProvider(), WAYBACK)

    def test_a_raising_provider_still_leaves_an_attempt_record(self):
        provider = Mock(**{'attempt_recovery.side_effect': RuntimeError('boom')})
        service = recovery_service({RecoverySource.ARCTIC_SHIFT: provider})

        result = service.attempt_recovery(REDDIT_URL)

        self.assertFalse(result.success)
        service.cache_manager.record_attempt.assert_called_once()
        service.cache_manager.cache_result.assert_not_called()


class TestFailedLookupIsNotCached(unittest.TestCase):
    def test_service_does_not_negative_cache_a_real_provider_outage(self):
        provider = ArcticShiftProvider(
            client=ArcticShiftClient(session=fake_session(fake_response(503, reason='Service Unavailable'))),
        )
        service = recovery_service({RecoverySource.ARCTIC_SHIFT: provider})

        with patch(ARCTIC):
            result = service.attempt_recovery(REDDIT_URL)

        self.assertFalse(result.success)
        service.cache_manager.cache_result.assert_not_called()


class TestArcticShiftBatchCallers(unittest.TestCase):
    def test_client_error_keeps_records_from_other_batches(self):
        client = ArcticShiftClient(
            session=fake_session(
                fake_response(payload={'data': [{'id': 'a1'}]}),
                fake_response(503, reason='Service Unavailable'),
            ),
            batch_size=1,
        )
        with patch(ARCTIC), self.assertRaises(ArchiveLookupError) as caught:
            client.fetch_posts(['a1', 'b2'])

        self.assertEqual(list(caught.exception.records), ['a1'])

    def test_gdpr_keeps_partial_records_and_falls_back_for_the_rest(self):
        client = SimpleNamespace(
            fetch_posts=Mock(side_effect=ArchiveLookupError('boom', {'a1': {'id': 'a1'}})),
        )
        fallback = Mock(**{'fetch_posts.return_value': {'b2': {'id': 'b2'}}})

        records = _fetch_from_archive(['a1', 'b2'], 'posts', client, fallback)

        self.assertEqual(set(records), {'a1', 'b2'})
        fallback.fetch_posts.assert_called_once_with(['b2'])

    def test_gdpr_keeps_what_the_fallback_found_before_it_failed(self):
        def first_batch_then_outage():
            provider = PullPushProvider()
            provider.session = fake_session(
                fake_response(payload={'data': [{'id': 'b2'}]}),
                fake_response(503, reason='Service Unavailable'),
            )
            return provider

        ids = [f'id{number}' for number in range(150)] + ['b2']
        client = SimpleNamespace(fetch_comments=Mock(return_value={}))

        with patch(PULLPUSH) as manager:
            manager.acquire.return_value = True
            with self.assertRaises(ArchiveLookupError) as caught:
                first_batch_then_outage().fetch_comments(ids)
            records = _fetch_from_archive(ids, 'comments', client, first_batch_then_outage())

        self.assertEqual(list(caught.exception.records), ['b2'])
        self.assertEqual(list(records), ['b2'])


class TestPullPushBatchRetryAfter(unittest.TestCase):
    def test_batch_lookup_passes_retry_after_to_the_rate_limiter(self):
        provider = PullPushProvider()
        provider.session = fake_session(fake_response(429, reason='Too Many Requests', headers={'Retry-After': '45'}))

        with patch(PULLPUSH) as manager:
            manager.acquire.return_value = True
            with self.assertRaises(ArchiveLookupError):
                provider.fetch_posts(['abc'])

        manager.report_response.assert_called_once_with('pullpush_io', 429, 45)


class TestLookalikeHosts(unittest.TestCase):
    def test_pullpush_rejects_lookalike_reddit_hosts(self):
        provider = PullPushProvider()

        for host in ('reddit.com.attacker.example', 'notreddit.com', 'redd.it.attacker.example'):
            self.assertIsNone(provider._parse_reddit_url(f'https://{host}/comments/abc123/'), host)
        self.assertIsNotNone(provider._parse_reddit_url(REDDIT_URL))

    def test_video_url_is_not_built_for_a_lookalike_host(self):
        for url, expected in (
            ('https://v.redd.it.attacker.example/abc', 'https://v.redd.it.attacker.example/abc'),
            ('https://v.redd.it/abc', 'https://v.redd.it/abc/DASH_720.mp4'),
        ):
            submission = SimpleNamespace(id='x', url=url, media=None, secure_media=None)
            self.assertEqual(_get_video_download_url(submission), expected)


class TestLookalikeVideoHosts(unittest.TestCase):
    def test_is_video_url_needs_a_real_v_redd_it_host(self):
        for url in (
            'https://v.redd.it.attacker.example/abc',
            'https://notv.redd.it/abc',
            'https://v.redd.it@attacker.example/abc',
        ):
            self.assertFalse(_is_video_url(url), url)
        self.assertTrue(_is_video_url('https://v.redd.it/abc'))
        self.assertTrue(_is_video_url('https://v.redd.it:443/abc'))

    def test_reddit_download_routes_on_the_exact_host(self):
        downloader = RedditMediaDownloader()
        for url, route in (
            ('https://v.redd.it/x', '_download_reddit_video'),
            ('https://i.redd.it/x', '_download_with_reddit_headers'),
            ('https://preview.redd.it/x', '_download_reddit_preview'),
            ('https://external-preview.redd.it/x', '_download_reddit_preview'),
            ('https://v.redd.it.i.redd.it/x', '_download_with_reddit_headers'),
            ('https://i.redd.it.preview.redd.it/x', '_download_reddit_preview'),
            ('https://preview.redd.it.i.redd.it/x', '_download_with_reddit_headers'),
        ):
            # Every route is mocked, because the preview route itself calls the headers route.
            routes = {name: Mock(return_value=name) for name in (
                '_download_reddit_video', '_download_with_reddit_headers', '_download_reddit_preview',
            )}
            with self.subTest(url=url), patch.multiple(downloader, **routes):
                self.assertEqual(downloader.download(url, 'unused'), route)

    def test_saved_extension_is_not_picked_by_substring(self):
        with tempfile.TemporaryDirectory() as save_dir, patch(
            'utils.media_services.download_manager.MediaDownloadManager.download_media',
            side_effect=lambda _url, path: DownloadResult(status=DownloadStatus.SUCCESS, local_path=path),
        ):
            self.assertTrue(download_media_into('https://v.redd.it/abc', save_dir, 'a').local_path.endswith('.mp4'))
            self.assertTrue(download_media_into('https://v.redd.it.attacker.example/abc', save_dir, 'b').local_path.endswith('.jpg'))


class TestFailureFromException(unittest.TestCase):
    def test_failure_names_the_source_and_error(self):
        result = RecoveryResult.failure_from_exception(ValueError('bad'), RecoverySource.PULLPUSH_IO, 0)

        self.assertEqual(result.source, RecoverySource.PULLPUSH_IO)
        self.assertIn('pullpush_io lookup failed: bad', result.error_message)


class TestCacheSchemaMigration(unittest.TestCase):
    def test_database_from_before_the_added_columns_is_upgraded(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, 'old.db')
            connection = sqlite3.connect(path)
            connection.execute(
                """CREATE TABLE recovery_cache (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, url_hash TEXT NOT NULL, original_url TEXT NOT NULL,
                    recovery_source TEXT NOT NULL, recovered_url TEXT, content_quality TEXT,
                    cached_at REAL NOT NULL, expires_at REAL NOT NULL, metadata_json TEXT,
                    UNIQUE(url_hash, recovery_source))"""
            )
            connection.execute(
                "INSERT INTO recovery_cache (url_hash, original_url, recovery_source, cached_at, expires_at) "
                "VALUES ('h', 'u', 's', 7, 9)"
            )
            connection.commit()
            connection.close()

            RecoveryCacheManager(path)
            RecoveryCacheManager(path)  # second open must be a no-op

            connection = sqlite3.connect(path)
            row = connection.execute("SELECT last_accessed_at, success FROM recovery_cache").fetchone()
            connection.close()

        self.assertEqual(row, (7, 1))


if __name__ == '__main__':
    unittest.main()
