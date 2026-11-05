"""Tests for current token-bucket rate limiting behavior."""

import time
import unittest
from unittest.mock import Mock, call, patch

import requests

from utils.content_recovery.providers.pullpush_provider import PullPushProvider
from utils.content_recovery.providers.wayback_provider import WaybackMachineProvider
from tests.media_helpers import media_manager
from utils.rate_limiter import (
    RateLimitConfig,
    ServiceRateLimitManager,
    TokenBucketRateLimiter,
    parse_retry_after,
    rate_limit_manager,
    rate_limited,
)


class TestTokenBucketRateLimiter(unittest.TestCase):
    def setUp(self):
        self.config = RateLimitConfig(max_requests_per_minute=60, burst_capacity=2)
        self.limiter = TokenBucketRateLimiter(self.config)

    def test_initial_burst_is_available(self):
        self.assertTrue(self.limiter.acquire(timeout=0))
        self.assertTrue(self.limiter.acquire(timeout=0))
        self.assertFalse(self.limiter.acquire(timeout=0))

    def test_tokens_refill_after_elapsed_time(self):
        self.limiter.acquire(timeout=0)
        self.limiter.acquire(timeout=0)
        self.limiter.state.last_refill = time.time() - 1

        self.assertTrue(self.limiter.acquire(timeout=0))

    def test_rate_limit_response_blocks_requests_until_reset(self):
        now = time.time()
        with patch('utils.rate_limiter.time.time', return_value=now):
            self.limiter.report_response(429, retry_after=60)

        with patch('utils.rate_limiter.time.time', return_value=now + 59):
            self.assertFalse(self.limiter.can_proceed())
            self.assertTrue(self.limiter.state.is_rate_limited)

        with patch('utils.rate_limiter.time.time', return_value=now + 60):
            self.assertTrue(self.limiter.can_proceed())
            self.assertFalse(self.limiter.state.is_rate_limited)

    def test_success_response_restarts_the_429_backoff(self):
        self.limiter.report_response(429)
        self.limiter.report_response(200)

        self.assertEqual(self.limiter.state.consecutive_failures, 0)

    def test_server_error_does_not_lengthen_a_later_429_backoff(self):
        with patch('utils.rate_limiter.time.time', return_value=1000.0):
            self.limiter.report_response(500)
            self.assertTrue(self.limiter.can_proceed())
            self.limiter.report_response(429)

        self.assertEqual(self.limiter.state.rate_limit_reset_time, 1000.0 + 60 * 1.5)


class TestPolicyTableOwnsTheLimits(unittest.TestCase):
    def test_starting_the_downloaders_keeps_the_tuned_limiters(self):
        names = ('imgur', 'reddit', 'generic')
        before = [rate_limit_manager._get_limiter(name) for name in names]
        manager = media_manager()

        manager._initialize_services()

        self.assertEqual([rate_limit_manager._get_limiter(name) for name in names], before)
        self.assertIsNotNone(manager._imgur_downloader)
        config = rate_limit_manager._get_limiter('imgur').config
        self.assertEqual((config.burst_capacity, config.retry_after_seconds, config.backoff_multiplier), (2, 120, 2.0))
        self.assertIsNone(rate_limit_manager._get_limiter('reddit_video'))


class TestServiceRateLimitManager(unittest.TestCase):
    def setUp(self):
        self.manager = ServiceRateLimitManager()
        self.manager.register_service(
            'images',
            RateLimitConfig(max_requests_per_minute=60, burst_capacity=1),
        )

    def test_registered_service_uses_its_own_limiter(self):
        self.assertTrue(self.manager.acquire('images', timeout=0))
        self.assertFalse(self.manager.acquire('images', timeout=0))

    def test_unknown_service_is_not_blocked(self):
        self.assertTrue(self.manager.acquire('unknown', timeout=0))


class TestInBackoff(unittest.TestCase):
    def setUp(self):
        self.limiter = TokenBucketRateLimiter(
            RateLimitConfig(max_requests_per_minute=60, burst_capacity=1)
        )

    def test_empty_token_bucket_is_not_backoff(self):
        self.assertTrue(self.limiter.acquire(timeout=0))
        self.assertFalse(self.limiter.can_proceed())
        self.assertFalse(self.limiter.in_backoff())

    def test_backoff_lasts_until_the_reset_time(self):
        self.limiter.report_response(429, retry_after=45)
        self.assertTrue(self.limiter.in_backoff())

        self.limiter.state.rate_limit_reset_time = time.time() - 1
        self.assertFalse(self.limiter.in_backoff())

    def test_manager_does_not_block_unknown_service(self):
        self.assertFalse(ServiceRateLimitManager().in_backoff('unknown'))


class TestRateLimitedDecorator(unittest.TestCase):
    def test_acquire_timeout_raises_without_calling_the_function(self):
        func = Mock()

        with patch('utils.rate_limiter.rate_limit_manager') as manager:
            manager.acquire.return_value = False
            with self.assertRaisesRegex(RuntimeError, "Rate limit timeout for service 'svc'"):
                rate_limited('svc', timeout=7)(func)()

        manager.acquire.assert_called_once_with('svc', 7)
        func.assert_not_called()


class TestParseRetryAfter(unittest.TestCase):
    def test_seconds_value_is_parsed(self):
        self.assertEqual(parse_retry_after(Mock(headers={'retry-after': '45'})), 45)

    def test_http_date_value_is_ignored(self):
        response = Mock(headers={'retry-after': 'Wed, 21 Oct 2026 07:28:00 GMT'})
        self.assertIsNone(parse_retry_after(response))

    def test_absent_header_is_none(self):
        self.assertIsNone(parse_retry_after(Mock(headers={})))


class TestProviderStatusReporting(unittest.TestCase):
    def _rate_limited_session(self, headers):
        response = Mock(status_code=429, headers=headers)
        response.raise_for_status.side_effect = requests.exceptions.HTTPError('429')
        return Mock(**{'get.return_value': response})

    def test_wayback_reports_rate_limit_responses(self):
        provider = WaybackMachineProvider()
        provider.session = self._rate_limited_session({'retry-after': '45'})

        with patch(
            'utils.content_recovery.providers.wayback_provider.rate_limit_manager',
        ) as manager:
            with self.assertRaises(requests.exceptions.HTTPError):
                provider._lookup_availability('https://example.com/image.jpg')
            with self.assertRaises(requests.exceptions.HTTPError):
                provider._search_cdx('https://example.com/image.jpg')

        self.assertEqual(
            manager.report_response.call_args_list,
            [call('wayback_machine', 429, 45)] * 2,
        )

    def test_pullpush_reports_rate_limit_responses(self):
        provider = PullPushProvider()
        provider.session = self._rate_limited_session({})

        with patch(
            'utils.content_recovery.providers.pullpush_provider.rate_limit_manager',
        ) as manager:
            with self.assertRaises(requests.exceptions.HTTPError):
                provider._search_submission('abc')
            with self.assertRaises(requests.exceptions.HTTPError):
                provider._search_comment('abc')

        self.assertEqual(
            manager.report_response.call_args_list,
            [call('pullpush_io', 429, None)] * 2,
        )


if __name__ == '__main__':
    unittest.main()
