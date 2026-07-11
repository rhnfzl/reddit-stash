"""Tests for parallel recovery result selection."""

import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from utils.content_recovery.recovery_metadata import (
    RecoveryMetadata,
    RecoveryQuality,
    RecoveryResult,
    RecoverySource,
)
from utils.content_recovery.recovery_service import ContentRecoveryService


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


class TestParallelRecoverySelection(unittest.TestCase):
    def _service(self, providers):
        service = ContentRecoveryService.__new__(ContentRecoveryService)
        service.providers = providers
        service._logger = SimpleNamespace(
            debug=lambda *_: None,
            error=lambda *_: None,
            info=lambda *_: None,
            warning=lambda *_: None,
        )
        service._record_attempt = lambda *_: None
        return service

    def test_selects_best_result_not_first_completed_result(self):
        low_quality = _result(RecoverySource.REDDIT_PREVIEWS, RecoveryQuality.THUMBNAIL)
        high_quality = _result(RecoverySource.WAYBACK_MACHINE, RecoveryQuality.HIGH_QUALITY)
        service = self._service({
            RecoverySource.REDDIT_PREVIEWS: _Provider(low_quality),
            RecoverySource.WAYBACK_MACHINE: _Provider(high_quality, delay=0.01),
        })

        result = service._attempt_parallel_recovery('https://example.com/image.jpg', None)

        self.assertTrue(result.success)
        self.assertEqual(result.metadata.source, RecoverySource.WAYBACK_MACHINE)

    def test_returns_failure_when_no_provider_recovers_content(self):
        service = self._service({
            RecoverySource.WAYBACK_MACHINE: _Provider(RecoveryResult.failure_result('missing')),
            RecoverySource.REDDIT_PREVIEWS: _Provider(RecoveryResult.failure_result('missing')),
        })

        result = service._attempt_parallel_recovery('https://example.com/image.jpg', None)

        self.assertFalse(result.success)
        self.assertEqual(result.error_message, 'All recovery providers failed')

    def test_failure_result_records_its_provider_source(self):
        result = RecoveryResult.failure_result('missing', RecoverySource.WAYBACK_MACHINE)

        self.assertEqual(result.attempted_sources, frozenset({RecoverySource.WAYBACK_MACHINE}))

    def test_success_preserves_all_completed_provider_sources(self):
        service = self._service({
            RecoverySource.WAYBACK_MACHINE: _Provider(RecoveryResult.failure_result('missing')),
            RecoverySource.REDDIT_PREVIEWS: _Provider(
                _result(RecoverySource.REDDIT_PREVIEWS, RecoveryQuality.THUMBNAIL),
            ),
        })

        result = service._attempt_sequential_recovery('https://example.com/image.jpg', None)

        self.assertTrue(result.success)
        self.assertEqual(
            result.attempted_sources,
            frozenset({
                RecoverySource.WAYBACK_MACHINE,
                RecoverySource.REDDIT_PREVIEWS,
            }),
        )

    def test_rate_limited_pullpush_is_not_started(self):
        provider = _Provider(_result(RecoverySource.PULLPUSH_IO, RecoveryQuality.MEDIUM_QUALITY))
        service = self._service({RecoverySource.PULLPUSH_IO: provider})

        with patch(
            'utils.content_recovery.recovery_service.rate_limit_manager.can_proceed',
            return_value=False,
        ):
            result = service._attempt_parallel_recovery('https://example.com/image.jpg', None)

        self.assertFalse(result.success)
        self.assertEqual(provider.attempts, 0)

    def test_sequential_rate_limited_pullpush_is_not_started(self):
        provider = _Provider(_result(RecoverySource.PULLPUSH_IO, RecoveryQuality.MEDIUM_QUALITY))
        service = self._service({RecoverySource.PULLPUSH_IO: provider})

        with patch(
            'utils.content_recovery.recovery_service.rate_limit_manager.can_proceed',
            return_value=False,
        ):
            result = service._attempt_sequential_recovery('https://example.com/image.jpg', None)

        self.assertFalse(result.success)
        self.assertEqual(provider.attempts, 0)

    def test_parallel_timeout_returns_without_waiting_for_running_provider(self):
        provider = _Provider(
            _result(RecoverySource.WAYBACK_MACHINE, RecoveryQuality.HIGH_QUALITY),
            delay=0.1,
        )
        service = self._service({RecoverySource.WAYBACK_MACHINE: provider})
        service._parallel_timeout_seconds = 0.01

        started_at = time.monotonic()
        result = service._attempt_parallel_recovery('https://example.com/image.jpg', None)
        duration = time.monotonic() - started_at

        self.assertFalse(result.success)
        self.assertLess(duration, 0.05)

    def test_provider_timeout_configures_parallel_timeout(self):
        service = ContentRecoveryService.__new__(ContentRecoveryService)
        service.config = SimpleNamespace(
            get_recovery_config=lambda: {
                'timeout_seconds': 120,
                'use_wayback_machine': False,
                'use_arctic_shift': False,
                'use_pushshift_api': False,
            },
        )
        service._logger = SimpleNamespace(debug=lambda *_: None, warning=lambda *_: None)

        service._init_providers()

        self.assertEqual(service._parallel_timeout_seconds, 120)


if __name__ == '__main__':
    unittest.main()
