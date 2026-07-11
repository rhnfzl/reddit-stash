"""Tests for current token-bucket rate limiting behavior."""

import time
import unittest
from unittest.mock import patch

from utils.rate_limiter import (
    RateLimitConfig,
    ServiceRateLimitManager,
    TokenBucketRateLimiter,
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

    def test_success_response_clears_failure_backoff(self):
        self.limiter.report_response(500)
        self.assertGreater(self.limiter.state.current_backoff, 0)

        self.limiter.report_response(200)

        self.assertEqual(self.limiter.state.consecutive_failures, 0)
        self.assertEqual(self.limiter.state.current_backoff, 0)


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

    def test_reset_service_restores_burst_capacity(self):
        self.assertTrue(self.manager.acquire('images', timeout=0))
        self.assertFalse(self.manager.acquire('images', timeout=0))

        self.assertTrue(self.manager.reset_service('images'))
        self.assertTrue(self.manager.acquire('images', timeout=0))

    def test_service_status_reports_registered_limiter(self):
        status = self.manager.get_service_status('images')

        self.assertIsNotNone(status)
        self.assertEqual(status['max_tokens'], 1)


if __name__ == '__main__':
    unittest.main()
