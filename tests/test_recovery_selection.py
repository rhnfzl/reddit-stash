"""Tests for parallel recovery result selection."""

import threading
import time
import unittest
from concurrent.futures import as_completed
from unittest.mock import patch

from utils.content_recovery.recovery_metadata import (
    RecoveryMetadata,
    RecoveryQuality,
    RecoveryResult,
    RecoverySource,
)
from tests.recovery_helpers import recovery_service

URL = 'https://example.com/image.jpg'


class _Provider:
    def __init__(self, result, delay=0):
        self.result = result
        self.delay = delay
        self.attempts = 0

    def attempt_recovery(self, _):
        self.attempts += 1
        if self.delay:
            time.sleep(self.delay)
        return self.result


def _result(source, quality):
    metadata = RecoveryMetadata(
        source=source,
        recovered_url=f'https://archive.example/{source.value}',
        recovery_timestamp=time.time(),
        content_quality=quality,
    )
    return RecoveryResult.success_result(metadata.recovered_url, metadata)


class _BlockedProvider:
    """Stays inside attempt_recovery until the test releases it."""

    def __init__(self, release):
        self.release = release
        self.finished = threading.Event()

    def attempt_recovery(self, _):
        self.release.wait(30)
        self.finished.set()
        return RecoveryResult.failure_result('missing')


class TestRecoverySelection(unittest.TestCase):
    def test_selects_best_result_not_first_completed_result(self):
        low_quality = _result(RecoverySource.ARCTIC_SHIFT, RecoveryQuality.LOW_QUALITY)
        high_quality = _result(RecoverySource.WAYBACK_MACHINE, RecoveryQuality.HIGH_QUALITY)
        service = recovery_service({
            RecoverySource.ARCTIC_SHIFT: _Provider(low_quality),
            RecoverySource.WAYBACK_MACHINE: _Provider(high_quality, delay=0.01),
        })

        result = service.attempt_recovery(URL)

        self.assertTrue(result.success)
        self.assertEqual(result.metadata.source, RecoverySource.WAYBACK_MACHINE)

    def test_equal_quality_is_settled_by_provider_reliability(self):
        wayback = _result(RecoverySource.WAYBACK_MACHINE, RecoveryQuality.HIGH_QUALITY)
        pullpush = _result(RecoverySource.PULLPUSH_IO, RecoveryQuality.HIGH_QUALITY)

        for wayback_delay, pullpush_delay in ((0.01, 0), (0, 0.01)):
            service = recovery_service({
                RecoverySource.WAYBACK_MACHINE: _Provider(wayback, delay=wayback_delay),
                RecoverySource.PULLPUSH_IO: _Provider(pullpush, delay=pullpush_delay),
            })
            with patch(
                'utils.content_recovery.recovery_service.rate_limit_manager.can_proceed',
                return_value=True,
            ):
                result = service.attempt_recovery(URL)

            self.assertEqual(result.metadata.source, RecoverySource.WAYBACK_MACHINE)

    def test_returns_failure_when_no_provider_recovers_content(self):
        service = recovery_service({
            RecoverySource.WAYBACK_MACHINE: _Provider(RecoveryResult.failure_result('missing')),
            RecoverySource.ARCTIC_SHIFT: _Provider(RecoveryResult.failure_result('missing')),
        })

        result = service.attempt_recovery(URL)

        self.assertFalse(result.success)
        self.assertEqual(result.error_message, 'All recovery providers failed')

    def test_failure_result_records_its_provider_source(self):
        result = RecoveryResult.failure_result('missing', RecoverySource.WAYBACK_MACHINE)

        self.assertEqual(result.attempted_sources, frozenset({RecoverySource.WAYBACK_MACHINE}))

    def test_success_preserves_all_completed_provider_sources(self):
        service = recovery_service({
            RecoverySource.WAYBACK_MACHINE: _Provider(RecoveryResult.failure_result('missing')),
            RecoverySource.ARCTIC_SHIFT: _Provider(
                _result(RecoverySource.ARCTIC_SHIFT, RecoveryQuality.LOW_QUALITY),
            ),
        })

        result = service.attempt_recovery(URL)

        self.assertTrue(result.success)
        self.assertEqual(
            result.attempted_sources,
            frozenset({
                RecoverySource.WAYBACK_MACHINE,
                RecoverySource.ARCTIC_SHIFT,
            }),
        )

    def test_rate_limited_pullpush_is_not_started(self):
        pullpush = _Provider(_result(RecoverySource.PULLPUSH_IO, RecoveryQuality.MEDIUM_QUALITY))
        arctic = _Provider(RecoveryResult.failure_result('missing'))
        service = recovery_service({RecoverySource.PULLPUSH_IO: pullpush, RecoverySource.ARCTIC_SHIFT: arctic})

        with patch(
            'utils.content_recovery.recovery_service.rate_limit_manager.can_proceed',
            return_value=False,
        ):
            result = service.attempt_recovery(URL)

            self.assertFalse(result.success)
            self.assertEqual(pullpush.attempts, 0)
            self.assertEqual(arctic.attempts, 1)

    def test_wayback_in_backoff_is_not_started(self):
        wayback = _Provider(_result(RecoverySource.WAYBACK_MACHINE, RecoveryQuality.HIGH_QUALITY))
        arctic = _Provider(_result(RecoverySource.ARCTIC_SHIFT, RecoveryQuality.MEDIUM_QUALITY))
        service = recovery_service({
            RecoverySource.WAYBACK_MACHINE: wayback,
            RecoverySource.ARCTIC_SHIFT: arctic,
        })

        with patch(
            'utils.content_recovery.recovery_service.rate_limit_manager.in_backoff',
            return_value=True,
        ):
            result = service.attempt_recovery(URL)

            self.assertEqual(result.metadata.source, RecoverySource.ARCTIC_SHIFT)
            self.assertEqual(wayback.attempts, 0)

    def test_wayback_with_only_an_empty_token_bucket_is_still_started(self):
        wayback = _Provider(_result(RecoverySource.WAYBACK_MACHINE, RecoveryQuality.HIGH_QUALITY))
        service = recovery_service({RecoverySource.WAYBACK_MACHINE: wayback})

        with patch(
            'utils.content_recovery.recovery_service.rate_limit_manager.can_proceed',
            return_value=False,
        ), patch(
            'utils.content_recovery.recovery_service.rate_limit_manager.in_backoff',
            return_value=False,
        ):
            result = service.attempt_recovery(URL)

            self.assertTrue(result.success)
            self.assertEqual(wayback.attempts, 1)

    def test_parallel_timeout_returns_without_waiting_for_running_provider(self):
        release = threading.Event()
        self.addCleanup(release.set)
        providers = [_BlockedProvider(release), _BlockedProvider(release)]
        service = recovery_service(
            dict(zip((RecoverySource.WAYBACK_MACHINE, RecoverySource.ARCTIC_SHIFT), providers)),
            timeout_seconds=0.01,
        )

        result = service.attempt_recovery(URL)

        self.assertFalse(result.success)
        self.assertFalse(any(provider.finished.is_set() for provider in providers))

    def test_provider_timeout_configures_parallel_timeout(self):
        service = recovery_service(
            {
                RecoverySource.WAYBACK_MACHINE: _Provider(RecoveryResult.failure_result('missing')),
                RecoverySource.ARCTIC_SHIFT: _Provider(RecoveryResult.failure_result('missing')),
            },
            timeout_seconds=120,
        )

        with patch(
            'utils.content_recovery.recovery_service.as_completed',
            wraps=as_completed,
        ) as completed:
            service.attempt_recovery(URL)

        self.assertEqual(completed.call_args.kwargs['timeout'], 120)


if __name__ == '__main__':
    unittest.main()
