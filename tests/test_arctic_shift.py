"""Unit tests for the Arctic Shift archive client and recovery provider."""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from utils.content_recovery.arctic_shift import ArcticShiftClient
from utils.content_recovery.providers.arctic_shift_provider import ArcticShiftProvider
from utils.content_recovery.recovery_metadata import RecoveryQuality, RecoverySource
from utils.content_recovery.recovery_service import ContentRecoveryService


class _Response:
    def __init__(self, data, status_code=200):
        self._data = data
        self.status_code = status_code
        self.headers = {}

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f'HTTP {self.status_code}')


class _Session:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []
        self.headers = {}

    def get(self, url, params, timeout):
        self.calls.append((url, params, timeout))
        return next(self.responses)


class _ArchiveClient:
    def __init__(self, posts=None, comments=None):
        self.posts = posts or {}
        self.comments = comments or {}
        self.post_ids = []
        self.comment_ids = []

    def fetch_posts(self, ids):
        self.post_ids.extend(ids)
        return self.posts

    def fetch_comments(self, ids):
        self.comment_ids.extend(ids)
        return self.comments


class TestArcticShiftClient(unittest.TestCase):
    def test_post_lookup_batches_ids_and_maps_results_by_base_id(self):
        session = _Session([
            _Response({'data': [{'id': 'a1', 'title': 'First'}]}),
            _Response({'data': [{'id': 'b2', 'title': 'Second'}]}),
        ])
        client = ArcticShiftClient(session=session, batch_size=1)

        records = client.fetch_posts(['t3_a1', 'b2'])

        self.assertEqual(records['a1']['title'], 'First')
        self.assertEqual(records['b2']['title'], 'Second')
        self.assertEqual(len(session.calls), 2)
        self.assertEqual(session.calls[0][1]['ids'], 'a1')
        self.assertEqual(session.calls[1][1]['ids'], 'b2')
        self.assertIn('title', session.calls[0][1]['fields'])

    def test_invalid_archive_response_returns_no_records(self):
        session = _Session([_Response({'unexpected': []})])
        client = ArcticShiftClient(session=session)

        self.assertEqual(client.fetch_comments(['t1_comment']), {})

    def test_non_object_archive_payload_returns_no_records(self):
        session = _Session([_Response([])])
        client = ArcticShiftClient(session=session)

        self.assertEqual(client.fetch_comments(['t1_comment']), {})


class TestArcticShiftRecoveryProvider(unittest.TestCase):
    def test_submission_recovery_returns_archived_metadata_without_media_url(self):
        client = _ArchiveClient(posts={
            'abc123': {
                'id': 'abc123',
                'title': 'Archived title',
                'selftext': 'Archived body',
                'subreddit': 'python',
            },
        })
        provider = ArcticShiftProvider(client=client)

        result = provider.attempt_recovery(
            'https://www.reddit.com/r/python/comments/abc123/example/',
        )

        self.assertTrue(result.success)
        self.assertIsNone(result.recovered_url)
        self.assertEqual(result.metadata.source, RecoverySource.ARCTIC_SHIFT)
        self.assertEqual(result.metadata.content_quality, RecoveryQuality.METADATA_ONLY)
        self.assertEqual(result.metadata.additional_metadata['title'], 'Archived title')
        self.assertEqual(client.post_ids, ['abc123'])

    def test_comment_permalink_uses_comment_lookup(self):
        client = _ArchiveClient(comments={
            'def456': {'id': 'def456', 'body': 'Archived comment'},
        })
        provider = ArcticShiftProvider(client=client)

        result = provider.attempt_recovery(
            'https://www.reddit.com/r/python/comments/abc123/example/def456/',
        )

        self.assertTrue(result.success)
        self.assertEqual(result.metadata.additional_metadata['body'], 'Archived comment')
        self.assertEqual(client.comment_ids, ['def456'])
        self.assertEqual(client.post_ids, [])

    def test_non_reddit_url_is_not_looked_up(self):
        client = _ArchiveClient()
        provider = ArcticShiftProvider(client=client)

        result = provider.attempt_recovery('https://example.com/image.jpg')

        self.assertFalse(result.success)
        self.assertEqual(client.post_ids, [])
        self.assertEqual(client.comment_ids, [])


class TestArcticShiftRecoveryServiceIntegration(unittest.TestCase):
    def test_recovery_service_registers_arctic_shift_before_pullpush(self):
        service = ContentRecoveryService.__new__(ContentRecoveryService)
        service.config = SimpleNamespace(
            get_recovery_config=lambda: {
                'timeout_seconds': 10,
                'use_wayback_machine': True,
                'use_arctic_shift': True,
                'use_pushshift_api': True,
                'use_reddit_previews': False,
                'use_reveddit_api': False,
            },
        )
        service._logger = SimpleNamespace(debug=lambda *_: None, warning=lambda *_: None)

        with patch('utils.content_recovery.recovery_service.WaybackMachineProvider'), patch(
            'utils.content_recovery.recovery_service.ArcticShiftProvider',
        ), patch('utils.content_recovery.recovery_service.PullPushProvider'):
            service._init_providers()

        self.assertEqual(
            list(service.providers),
            [
                RecoverySource.WAYBACK_MACHINE,
                RecoverySource.ARCTIC_SHIFT,
                RecoverySource.PULLPUSH_IO,
            ],
        )


if __name__ == '__main__':
    unittest.main()
