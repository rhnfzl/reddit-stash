"""Regression tests for recovery providers retired from active probing."""

import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from utils.content_recovery.providers.reddit_preview_provider import RedditPreviewProvider
from utils.content_recovery.recovery_service import ContentRecoveryService


class TestRedditPreviewProvider(unittest.TestCase):
    def test_does_not_construct_unverifiable_preview_urls(self):
        provider = RedditPreviewProvider()

        self.assertEqual(provider._generate_preview_candidates('https://example.com/image.jpg'), [])

    def test_external_url_does_not_trigger_preview_head_requests(self):
        provider = RedditPreviewProvider()
        provider.session = Mock()

        result = provider.attempt_recovery('https://example.com/image.jpg')

        self.assertFalse(result.success)
        provider.session.head.assert_not_called()


class TestRetiredRecoveryServices(unittest.TestCase):
    def test_recovery_service_never_registers_unsupported_recovery_probes(self):
        service = ContentRecoveryService.__new__(ContentRecoveryService)
        service.config = SimpleNamespace(
            get_recovery_config=lambda: {
                'timeout_seconds': 10,
                'use_wayback_machine': False,
                'use_arctic_shift': False,
                'use_pushshift_api': False,
                'use_reddit_previews': False,
                'use_reveddit_api': True,
            },
        )
        service._logger = Mock()

        service._init_providers()

        self.assertEqual(service.providers, {})
        self.assertEqual(service._logger.warning.call_count, 2)


if __name__ == '__main__':
    unittest.main()
