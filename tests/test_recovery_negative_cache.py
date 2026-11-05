"""Regression tests for short-lived failed recovery cache entries."""

import unittest
import tempfile
from types import SimpleNamespace
from unittest.mock import Mock, patch

from utils.content_recovery.cache_manager import RecoveryCacheManager
from utils.content_recovery.recovery_config import CacheConfig
from utils.content_recovery.recovery_metadata import (
    RecoveryAttempt,
    RecoveryMetadata,
    RecoveryQuality,
    RecoveryResult,
    RecoverySource,
)
from tests.recovery_helpers import recovery_service


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

    def cleanup_if_due(self):
        pass


def _cache_entry(source, recovered_url, success=None, metadata_json=None):
    if success is None:
        success = recovered_url is not None

    return SimpleNamespace(
        recovery_source=source,
        recovered_url=recovered_url,
        content_quality=RecoveryQuality.MEDIUM_QUALITY,
        cached_at=1,
        success=success,
        metadata_json=metadata_json,
    )


class TestRecoveryNegativeCache(unittest.TestCase):
    def test_cached_failure_does_not_hide_cached_success(self):
        url = 'https://example.com/image.jpg'
        cache_manager = _CacheManager({
            (url, RecoverySource.WAYBACK_MACHINE): _cache_entry(
                RecoverySource.WAYBACK_MACHINE,
                None,
            ),
            (url, RecoverySource.ARCTIC_SHIFT): _cache_entry(
                RecoverySource.ARCTIC_SHIFT,
                'https://preview.example/image.jpg',
            ),
        })
        service = recovery_service({
            RecoverySource.WAYBACK_MACHINE: _Provider(RecoveryResult.failure_result('missing')),
            RecoverySource.ARCTIC_SHIFT: _Provider(RecoveryResult.failure_result('missing')),
        }, cache_manager)

        result = service.attempt_recovery(url)

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
        wayback = _Provider(RecoveryResult.failure_result('missing'))
        arctic_shift = _Provider(RecoveryResult.failure_result('missing'))
        service = recovery_service({
            RecoverySource.WAYBACK_MACHINE: wayback,
            RecoverySource.ARCTIC_SHIFT: arctic_shift,
        }, cache_manager)

        result = service.attempt_recovery(url)

        self.assertEqual(result.error_message, 'All recovery providers failed')
        self.assertEqual((wayback.attempts, arctic_shift.attempts), (1, 1))

    def test_complete_negative_cache_short_circuits_provider_calls(self):
        url = 'https://example.com/image.jpg'
        cache_manager = _CacheManager({
            (url, RecoverySource.WAYBACK_MACHINE): _cache_entry(
                RecoverySource.WAYBACK_MACHINE,
                None,
            ),
            (url, RecoverySource.ARCTIC_SHIFT): _cache_entry(
                RecoverySource.ARCTIC_SHIFT,
                None,
            ),
        })
        wayback = _Provider(RecoveryResult.failure_result('missing'))
        arctic_shift = _Provider(RecoveryResult.failure_result('missing'))
        service = recovery_service({
            RecoverySource.WAYBACK_MACHINE: wayback,
            RecoverySource.ARCTIC_SHIFT: arctic_shift,
        }, cache_manager)

        result = service.attempt_recovery(url)

        self.assertFalse(result.success)
        self.assertEqual(result.error_message, 'Cached negative result')
        self.assertEqual(wayback.attempts, 0)
        self.assertEqual(arctic_shift.attempts, 0)

    def test_cached_metadata_only_success_preserves_archived_text(self):
        url = 'https://example.com/image.jpg'
        cache_manager = _CacheManager({
            (url, RecoverySource.PULLPUSH_IO): _cache_entry(
                RecoverySource.PULLPUSH_IO,
                None,
                success=True,
                metadata_json='{"body": "Archived text"}',
            ),
        })
        service = recovery_service({
            RecoverySource.PULLPUSH_IO: _Provider(RecoveryResult.failure_result('missing')),
        }, cache_manager)

        result = service.attempt_recovery(url)

        self.assertTrue(result.success)
        self.assertIsNone(result.recovered_url)
        self.assertEqual(result.metadata.additional_metadata['body'], 'Archived text')

    def test_caches_only_providers_that_completed_a_failed_recovery(self):
        cache_manager = _CacheManager()
        wayback = _Provider(
            RecoveryResult.failure_result('reworded archive answer', confirmed_miss=True)
        )
        service = recovery_service({RecoverySource.WAYBACK_MACHINE: wayback}, cache_manager)

        result = service.attempt_recovery('https://example.com/image.jpg')

        self.assertFalse(result.success)
        self.assertEqual(wayback.attempts, 1)
        self.assertEqual(len(cache_manager.cache_calls), 1)
        cache_call = cache_manager.cache_calls[0]
        self.assertEqual(cache_call['source'], RecoverySource.WAYBACK_MACHINE)
        self.assertIsNone(cache_call['recovered_url'])
        self.assertFalse(cache_call['success'])
        self.assertEqual(cache_call['quality'], RecoveryQuality.METADATA_ONLY)
        self.assertLess(cache_call['ttl_hours'], 24)

    def test_successful_recovery_is_cached_for_the_configured_duration(self):
        cache_manager = _CacheManager()
        url = 'https://example.com/image.jpg'
        metadata = RecoveryMetadata(
            source=RecoverySource.WAYBACK_MACHINE,
            recovered_url='https://archive.example/image.jpg',
            recovery_timestamp=1.0,
            content_quality=RecoveryQuality.HIGH_QUALITY,
        )
        wayback = _Provider(RecoveryResult.success_result(metadata.recovered_url, metadata))
        service = recovery_service(
            {RecoverySource.WAYBACK_MACHINE: wayback}, cache_manager, cache_duration_hours=7,
        )

        service.attempt_recovery(url)

        self.assertEqual(cache_manager.cache_calls[0]['ttl_hours'], 7)

    def test_does_not_cache_transient_provider_failure(self):
        cache_manager = _CacheManager()
        wayback = _Provider(
            # Reads like a miss, but only the flag may say the archive answered.
            RecoveryResult.failure_result('Content not found: request timed out after 10.00s')
        )
        service = recovery_service({RecoverySource.WAYBACK_MACHINE: wayback}, cache_manager)

        result = service.attempt_recovery('https://example.com/image.jpg')

        self.assertFalse(result.success)
        self.assertEqual(wayback.attempts, 1)
        self.assertEqual(cache_manager.cache_calls, [])

    def test_rate_limited_provider_is_not_cached_as_a_failure(self):
        cache_manager = _CacheManager()
        pullpush = _Provider(RecoveryResult.failure_result('missing'))
        service = recovery_service({RecoverySource.PULLPUSH_IO: pullpush}, cache_manager)

        with patch(
            'utils.content_recovery.recovery_service.rate_limit_manager.can_proceed',
            return_value=False,
        ):
            result = service.attempt_recovery('https://example.com/image.jpg')

        self.assertFalse(result.success)
        self.assertEqual(pullpush.attempts, 0)
        self.assertEqual(cache_manager.cache_calls, [])

    def test_rate_limited_provider_records_a_deferred_attempt(self):
        cache_manager = _CacheManager()
        service = recovery_service(
            {RecoverySource.PULLPUSH_IO: _Provider(RecoveryResult.failure_result('missing'))},
            cache_manager,
        )

        with patch(
            'utils.content_recovery.recovery_service.rate_limit_manager.can_proceed',
            return_value=False,
        ), patch.object(cache_manager, 'record_attempt') as record_attempt:
            service.attempt_recovery('https://example.com/image.jpg')

        attempt = record_attempt.call_args.args[0]
        self.assertIs(attempt.recovery_source, RecoverySource.PULLPUSH_IO)
        self.assertFalse(attempt.success)
        self.assertIn('deferred by rate limiter', attempt.error_message)

    def test_cache_preserves_metadata_only_success_state(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            cache_manager = RecoveryCacheManager(f'{temp_dir}/recovery.db')
            url = 'https://example.com/image.jpg'

            cached = cache_manager.cache_result(
                url=url,
                source=RecoverySource.PULLPUSH_IO,
                recovered_url=None,
                quality=RecoveryQuality.METADATA_ONLY,
                metadata={'body': 'Archived text'},
                success=True,
            )
            entry = cache_manager.get_cached_result(url, RecoverySource.PULLPUSH_IO)

        self.assertTrue(cached)
        self.assertIsNotNone(entry)
        self.assertTrue(entry.success)
        self.assertIsNone(entry.recovered_url)
        self.assertEqual(entry.metadata_json, '{"body": "Archived text"}')

    def test_cache_entry_stops_being_served_at_its_expiry_time(self):
        url = 'https://example.com/image.jpg'
        with tempfile.TemporaryDirectory() as temp_dir:
            cache_manager = RecoveryCacheManager(f'{temp_dir}/recovery.db')
            with patch('utils.content_recovery.cache_manager.time.time') as clock:
                clock.return_value = 1000.0
                cache_manager.cache_result(url, RecoverySource.PULLPUSH_IO, 'https://a/b', RecoveryQuality.LOW_QUALITY,
                                           ttl_hours=1)
                clock.return_value = 1000.0 + 3599
                before = cache_manager.get_cached_result(url, RecoverySource.PULLPUSH_IO)
                clock.return_value = 1000.0 + 3600
                at_expiry = cache_manager.get_cached_result(url, RecoverySource.PULLPUSH_IO)

        self.assertIsNotNone(before)
        self.assertIsNone(at_expiry)

    def _cache_manager(self, temp_dir, **settings):
        manager = RecoveryCacheManager(f'{temp_dir}/recovery.db')
        manager.config = CacheConfig(**settings)
        return manager

    def test_cleanup_runs_on_the_first_lookup_and_then_once_per_interval(self):
        cache = Mock(**{'get_cached_result.return_value': None})
        service = recovery_service({RecoverySource.PULLPUSH_IO: _Provider(None)}, cache)
        with patch(
            'utils.content_recovery.recovery_service.rate_limit_manager.can_proceed',
            return_value=False,
        ):
            service.attempt_recovery('https://example.com/image.jpg')
        cache.cleanup_if_due.assert_called_once()

        with tempfile.TemporaryDirectory() as temp_dir:
            manager = self._cache_manager(temp_dir, cleanup_interval_minutes=1)
            with patch.object(manager, 'cleanup_cache') as cleanup, \
                    patch('utils.content_recovery.cache_manager.time.monotonic') as clock:
                for now, expected_calls in ((1000.0, 1), (1059.0, 1), (1060.0, 2)):
                    clock.return_value = now
                    manager.cleanup_if_due()
                    self.assertEqual(cleanup.call_count, expected_calls)

    def test_disabled_cleanup_never_runs(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = self._cache_manager(temp_dir, enable_background_cleanup=False)
            with patch.object(manager, 'cleanup_cache') as cleanup:
                manager.cleanup_if_due()
        cleanup.assert_not_called()

    def test_negative_entries_count_toward_the_cache_size_limit(self):
        url = 'https://example.com/' + 'x' * (1024 * 1024)
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = self._cache_manager(temp_dir, max_cache_size_mb=0)
            manager.cache_result(url, RecoverySource.WAYBACK_MACHINE, None, RecoveryQuality.METADATA_ONLY, success=False)

            self.assertEqual(manager.cleanup_lru_cache(), 1)

    def test_count_limit_evicts_the_least_recently_used_entries(self):
        urls = [f'https://example.com/{i}.jpg' for i in range(4)]
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = self._cache_manager(temp_dir, max_cache_entries=2)
            with manager.sqlite_manager.get_connection() as conn:
                for age, url in enumerate(urls):
                    manager.cache_result(url, RecoverySource.WAYBACK_MACHINE, 'https://a/b', RecoveryQuality.LOW_QUALITY)
                    conn.execute('UPDATE recovery_cache SET last_accessed_at = ? WHERE original_url = ?', (age, url))
                conn.commit()
            manager.get_cached_result(urls[0], RecoverySource.WAYBACK_MACHINE)

            removed = manager.cleanup_lru_cache()
            with manager.sqlite_manager.get_connection() as conn:
                kept = {row[0] for row in conn.execute('SELECT original_url FROM recovery_cache')}

        self.assertEqual(removed, 2)
        self.assertEqual(kept, {urls[0], urls[3]})

    def test_cache_rows_keep_their_stored_enum_values(self):
        url = 'https://example.com/image.jpg'
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = RecoveryCacheManager(f'{temp_dir}/recovery.db')
            manager.cache_result(url, RecoverySource.PULLPUSH_IO, 'https://a/b', RecoveryQuality.LOW_QUALITY)
            manager.record_attempt(RecoveryAttempt(url, RecoverySource.PULLPUSH_IO))
            with manager.sqlite_manager.get_connection() as conn:
                cached = conn.execute('SELECT recovery_source, content_quality FROM recovery_cache').fetchone()
                attempted = conn.execute('SELECT recovery_source FROM recovery_attempts').fetchone()
            entry = manager.get_cached_result(url, RecoverySource.PULLPUSH_IO)

        self.assertEqual(cached, ('pullpush_io', 'low_quality'))
        self.assertEqual(attempted, ('pullpush_io',))
        self.assertIs(entry.recovery_source, RecoverySource.PULLPUSH_IO)
        self.assertIs(entry.content_quality, RecoveryQuality.LOW_QUALITY)


if __name__ == '__main__':
    unittest.main()
