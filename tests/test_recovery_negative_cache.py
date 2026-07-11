"""Regression tests for short-lived failed recovery cache entries."""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from utils.content_recovery.recovery_metadata import (
    RecoveryCacheEntry,
    RecoveryQuality,
    RecoveryResult,
    RecoverySource,
)
from utils.content_recovery.recovery_service import ContentRecoveryService


class _Provider:
    def __init__(self, result):
        self.result = result
        self.attempts = 0

    def attempt_recovery(self, _):
        self.attempts += 1
        return self.result


class _CacheManager:
    def __init__(self, entries=None):
        self.entries = entries or {}
        self.cache_calls = []

    def get_cached_result(self, url, source):
        return self.entries.get((url, source))

    def cache_result(self, **kwargs):
        self.cache_calls.append(kwargs)
        return True

    def record_attempt(self, _):
        return True


def _cache_entry(source, recovered_url):
    return SimpleNamespace(
        recovery_source=source.value,
        recovered_url=recovered_url,
        content_quality=RecoveryQuality.MEDIUM_QUALITY.value,
        cached_at=1,
        is_expired=False,
    )


class TestRecoveryNegativeCache(unittest.TestCase):
    def _service(self, providers, cache_manager=None):
        service = ContentRecoveryService.__new__(ContentRecoveryService)
        service.providers = providers
        service.cache_manager = cache_manager or _CacheManager()
        service.config = SimpleNamespace(
            get_recovery_config=lambda: {'cache_duration_hours': 24},
        )
        service._logger = SimpleNamespace(
            debug=lambda *_: None,
            error=lambda *_: None,
            info=lambda *_: None,
            warning=lambda *_: None,
        )
        service._stats = {
            'total_attempts': 0,
            'cache_hits': 0,
            'successful_recoveries': 0,
            'failed_recoveries': 0,
            'provider_stats': {},
        }
        return service

    def test_cached_failure_does_not_hide_cached_success(self):
        url = 'https://example.com/image.jpg'
        cache_manager = _CacheManager({
            (url, RecoverySource.WAYBACK_MACHINE): _cache_entry(
                RecoverySource.WAYBACK_MACHINE,
                None,
            ),
            (url, RecoverySource.REDDIT_PREVIEWS): _cache_entry(
                RecoverySource.REDDIT_PREVIEWS,
                'https://preview.example/image.jpg',
            ),
        })
        service = self._service({
            RecoverySource.WAYBACK_MACHINE: _Provider(RecoveryResult.failure_result('missing')),
            RecoverySource.REDDIT_PREVIEWS: _Provider(RecoveryResult.failure_result('missing')),
        }, cache_manager)

        result = service._check_cache(url)

        self.assertTrue(result.success)
        self.assertEqual(result.recovered_url, 'https://preview.example/image.jpg')

    def test_partial_negative_cache_retries_missing_provider(self):
        url = 'https://example.com/image.jpg'
        cache_manager = _CacheManager({
            (url, RecoverySource.WAYBACK_MACHINE): _cache_entry(
                RecoverySource.WAYBACK_MACHINE,
                None,
            ),
        })
        service = self._service({
            RecoverySource.WAYBACK_MACHINE: _Provider(RecoveryResult.failure_result('missing')),
            RecoverySource.REDDIT_PREVIEWS: _Provider(RecoveryResult.failure_result('missing')),
        }, cache_manager)

        self.assertIsNone(service._check_cache(url))

    def test_complete_negative_cache_short_circuits_provider_calls(self):
        url = 'https://example.com/image.jpg'
        cache_manager = _CacheManager({
            (url, RecoverySource.WAYBACK_MACHINE): _cache_entry(
                RecoverySource.WAYBACK_MACHINE,
                None,
            ),
            (url, RecoverySource.REDDIT_PREVIEWS): _cache_entry(
                RecoverySource.REDDIT_PREVIEWS,
                None,
            ),
        })
        wayback = _Provider(RecoveryResult.failure_result('missing'))
        previews = _Provider(RecoveryResult.failure_result('missing'))
        service = self._service({
            RecoverySource.WAYBACK_MACHINE: wayback,
            RecoverySource.REDDIT_PREVIEWS: previews,
        }, cache_manager)

        result = service.attempt_recovery(url, async_mode=False)

        self.assertFalse(result.success)
        self.assertEqual(result.error_message, 'Cached negative result')
        self.assertEqual(wayback.attempts, 0)
        self.assertEqual(previews.attempts, 0)

    def test_caches_only_providers_that_completed_a_failed_recovery(self):
        cache_manager = _CacheManager()
        wayback = _Provider(RecoveryResult.failure_result('missing'))
        service = self._service({RecoverySource.WAYBACK_MACHINE: wayback}, cache_manager)

        result = service.attempt_recovery('https://example.com/image.jpg', async_mode=False)

        self.assertFalse(result.success)
        self.assertEqual(wayback.attempts, 1)
        self.assertEqual(len(cache_manager.cache_calls), 1)
        cache_call = cache_manager.cache_calls[0]
        self.assertEqual(cache_call['source'], RecoverySource.WAYBACK_MACHINE)
        self.assertIsNone(cache_call['recovered_url'])
        self.assertEqual(cache_call['quality'], RecoveryQuality.METADATA_ONLY)
        self.assertLess(cache_call['ttl_hours'], 24)

    def test_rate_limited_provider_is_not_cached_as_a_failure(self):
        cache_manager = _CacheManager()
        pullpush = _Provider(RecoveryResult.failure_result('missing'))
        service = self._service({RecoverySource.PULLPUSH_IO: pullpush}, cache_manager)

        with patch(
            'utils.content_recovery.recovery_service.rate_limit_manager.can_proceed',
            return_value=False,
        ):
            result = service.attempt_recovery('https://example.com/image.jpg', async_mode=False)

        self.assertFalse(result.success)
        self.assertEqual(pullpush.attempts, 0)
        self.assertEqual(cache_manager.cache_calls, [])

    def test_cache_entry_expires_at_the_exact_expiry_time(self):
        entry = RecoveryCacheEntry(expires_at=100)

        with patch('utils.content_recovery.recovery_metadata.time.time', return_value=100):
            self.assertTrue(entry.is_expired)


if __name__ == '__main__':
    unittest.main()
