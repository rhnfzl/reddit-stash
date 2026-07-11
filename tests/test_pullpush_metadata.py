"""Regression tests for PullPush metadata-only recovery."""

import tempfile
import unittest
from unittest.mock import Mock, patch

from utils.content_recovery.providers.pullpush_provider import PullPushProvider
from utils.content_recovery.recovery_metadata import (
    RecoveryMetadata,
    RecoveryQuality,
    RecoveryResult,
    RecoverySource,
)
from utils.media_download_manager import MediaDownloadManager
from utils.service_abstractions import DownloadResult, DownloadStatus


class _FailingDownloader:
    def __init__(self):
        self.calls = []

    def download(self, url, _save_path):
        self.calls.append(url)
        return DownloadResult(
            status=DownloadStatus.FAILED,
            error_message='not found',
        )


class _Response:
    def __init__(self, data):
        self._data = data
        self.status_code = 200
        self.headers = {}

    def json(self):
        return self._data

    def raise_for_status(self):
        return None


class _Session:
    def __init__(self):
        self.headers = {}
        self.calls = []

    def get(self, url, params, timeout):
        self.calls.append((url, params, timeout))
        return _Response({'data': [{'id': 'abc123', 'title': 'Archived title'}]})


class TestPullPushMetadataRecovery(unittest.TestCase):
    def test_comment_permalink_uses_comment_lookup(self):
        parsed = PullPushProvider()._parse_reddit_url(
            'https://www.reddit.com/r/python/comments/abc123/example/def456/'
        )

        self.assertEqual(parsed['type'], 'comment')
        self.assertEqual(parsed['id'], 'def456')

    def test_batch_metadata_lookup_returns_records_by_id(self):
        provider = PullPushProvider()
        provider.session = _Session()

        records = provider.fetch_metadata_by_ids('posts', ['abc123'])

        self.assertEqual(records['abc123']['title'], 'Archived title')
        self.assertEqual(provider.session.calls[0][1]['ids'], 'abc123')

    def test_archived_submission_is_successful_metadata_without_media_url(self):
        provider = PullPushProvider()
        pullpush_data = {
            'type': 'submission',
            'id': 'abc123',
            'data': {
                'subreddit': 'python',
                'author': 'archive_user',
                'created_utc': 123,
                'score': 42,
                'title': 'Archived title',
                'selftext': 'Archived post body',
            },
        }

        result = provider._create_success_result(
            'https://www.reddit.com/r/python/comments/abc123/example/',
            pullpush_data,
            duration=0.1,
        )

        self.assertTrue(result.success)
        self.assertIsNone(result.recovered_url)
        self.assertEqual(result.metadata.content_quality, RecoveryQuality.METADATA_ONLY)
        self.assertEqual(result.metadata.additional_metadata['title'], 'Archived title')
        self.assertEqual(result.metadata.additional_metadata['selftext'], 'Archived post body')

    def test_archived_comment_preserves_body_without_constructing_permalink(self):
        provider = PullPushProvider()
        pullpush_data = {
            'type': 'comment',
            'id': 'def456',
            'data': {
                'subreddit': 'python',
                'author': 'archive_user',
                'body': 'Archived comment body',
            },
        }

        result = provider._create_success_result(
            'https://www.reddit.com/r/python/comments/abc123/example/def456/',
            pullpush_data,
            duration=0.1,
        )

        self.assertTrue(result.success)
        self.assertIsNone(result.recovered_url)
        self.assertEqual(result.metadata.additional_metadata['body'], 'Archived comment body')

    def test_media_manager_does_not_download_metadata_only_recovery(self):
        metadata = RecoveryMetadata(
            source=RecoverySource.PULLPUSH_IO,
            recovered_url=None,
            recovery_timestamp=1,
            content_quality=RecoveryQuality.METADATA_ONLY,
            additional_metadata={'body': 'Archived comment body'},
        )
        recovery_service = Mock()
        recovery_service.is_enabled.return_value = True
        recovery_service.attempt_recovery.return_value = RecoveryResult.success_result(None, metadata)
        downloader = _FailingDownloader()

        with tempfile.TemporaryDirectory() as save_path:
            manager = MediaDownloadManager()
            manager._retry_queue = Mock()
            manager._service_manager = Mock()
            manager._service_manager.execute_with_protection.side_effect = (
                lambda _name, operation, fallback_value: operation()
            )

            with patch.object(manager, '_recovery_service', recovery_service), patch.object(
                manager,
                '_get_service_for_url',
                return_value=('generic', downloader),
            ):
                result = manager.download_media(
                    'https://i.redd.it/failing_image.jpg',
                    save_path,
                )

        self.assertIsNone(result)
        self.assertEqual(downloader.calls, ['https://i.redd.it/failing_image.jpg'])


if __name__ == '__main__':
    unittest.main()
