"""
Comprehensive tests for PRAW helpers module.

Tests the safe iteration, error handling, and recovery integration
for Reddit content fetching.
"""

import unittest
from unittest.mock import Mock, patch
import prawcore

import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from utils.praw_helpers import (
    safe_fetch_items,
    safe_fetch_items_one_by_one,
    construct_reddit_url,
    create_recovery_metadata_markdown,
    RecoveredItem
)
from utils.content_recovery import RecoveryResult, RecoveryMetadata, RecoverySource, RecoveryQuality


class RaisingIterable:
    """An iterable that fails when the consumer starts iteration."""

    def __init__(self, error):
        self.error = error

    def __iter__(self):
        raise self.error


class TestConstructRedditUrl(unittest.TestCase):
    """Test URL construction from PRAW objects."""

    def test_construct_url_from_comment(self):
        """Test constructing URL from a Comment object."""
        comment = Mock()
        comment.permalink = "/r/test/comments/abc123/test_post/def456"

        url = construct_reddit_url(comment)
        self.assertEqual(url, "https://reddit.com/r/test/comments/abc123/test_post/def456")

    def test_construct_url_from_submission(self):
        """Test constructing URL from a Submission object."""
        submission = Mock()
        submission.permalink = "/r/test/comments/abc123/test_post"

        url = construct_reddit_url(submission)
        self.assertEqual(url, "https://reddit.com/r/test/comments/abc123/test_post")

    def test_construct_url_from_invalid_object(self):
        """Test handling of objects without permalink."""
        obj = Mock(spec=[])  # No permalink attribute

        url = construct_reddit_url(obj)
        self.assertIsNone(url)


class TestRecoveryMetadataMarkdown(unittest.TestCase):
    """Test recovery metadata markdown formatting."""

    def test_create_recovery_banner(self):
        """Test creating recovery metadata banner."""
        metadata = RecoveryMetadata(
            source=RecoverySource.WAYBACK_MACHINE,
            recovered_url="https://archive.org/test",
            recovery_timestamp=1700000000.0,
            content_quality=RecoveryQuality.HIGH_QUALITY
        )

        recovery_result = RecoveryResult.success_result("https://archive.org/test", metadata)

        banner = create_recovery_metadata_markdown(recovery_result)

        self.assertIn("Recovered Content", banner)
        self.assertIn("Wayback Machine", banner)
        self.assertIn("High Quality", banner)
        self.assertIn("https://archive.org/test", banner)

    def test_empty_banner_for_failed_recovery(self):
        """Test that failed recovery produces no banner."""
        recovery_result = RecoveryResult.failure_result("Not found")

        banner = create_recovery_metadata_markdown(recovery_result)

        self.assertEqual(banner, "")


class TestRecoveredItem(unittest.TestCase):
    """Test RecoveredItem placeholder class."""

    def test_recovered_item_creation(self):
        """Test creating a RecoveredItem."""
        metadata = RecoveryMetadata(
            source=RecoverySource.PULLPUSH_IO,
            recovered_url="https://pullpush.io/test",
            recovery_timestamp=1700000000.0,
            content_quality=RecoveryQuality.MEDIUM_QUALITY,
            additional_metadata={'author': 'testuser', 'body': 'Test comment'}
        )

        recovery_result = RecoveryResult.success_result("https://pullpush.io/test", metadata)

        item = RecoveredItem(
            item_type='comment',
            item_id='abc123',
            recovery_result=recovery_result,
            original_url='https://reddit.com/r/test/comments/abc123'
        )

        self.assertEqual(item.item_type, 'comment')
        self.assertEqual(item.id, 'abc123')
        self.assertTrue(item.is_recovered)
        self.assertEqual(item.recovered_data['author'], 'testuser')


class TestSafeFetchItems(unittest.TestCase):
    """Test safe fetching with error handling."""

    def test_successful_batch_fetch(self):
        """Test successful batch fetch (happy path)."""
        mock_items = [Mock(id=f'item_{i}') for i in range(5)]
        mock_generator = iter(mock_items)

        result = list(safe_fetch_items(mock_generator, 'test'))

        self.assertEqual(len(result), 5)
        self.assertEqual(result[0].id, 'item_0')

    def test_batch_fetch_404_skips_exhausted_generator(self):
        """Test that a failed batch fetch does not yield partial data."""
        result = list(safe_fetch_items(
            RaisingIterable(prawcore.exceptions.NotFound(Mock())), 'test'
        ))

        # Should return empty list since generator is exhausted
        self.assertEqual(len(result), 0)

    def test_forbidden_error_handling(self):
        """Test handling of Forbidden errors."""
        result = list(safe_fetch_items(
            RaisingIterable(prawcore.exceptions.Forbidden(Mock())), 'test'
        ))

        # Should return empty list and log error
        self.assertEqual(len(result), 0)


class TestSafeFetchItemsOneByOne(unittest.TestCase):
    """Test one-by-one fetching with recovery."""

    def test_successful_iteration(self):
        """Test successful one-by-one iteration."""
        mock_items = [Mock(id=f'item_{i}') for i in range(3)]
        mock_generator = iter(mock_items)

        result = list(safe_fetch_items_one_by_one(
            mock_generator,
            'test',
            recovery_enabled=False
        ))

        self.assertEqual(len(result), 3)

    def test_iterator_initialization_not_found_is_skipped(self):
        """Test that iterator initialization errors do not escape."""
        result = list(safe_fetch_items_one_by_one(
            RaisingIterable(prawcore.exceptions.NotFound(Mock())),
            'test',
            recovery_enabled=False,
        ))

        self.assertEqual(result, [])

    @patch('utils.praw_helpers.ContentRecoveryService')
    def test_not_found_stops_iteration_without_fabricating_an_item(self, mock_recovery_service_class):
        """Test that a mid-stream 404 keeps only confirmed items."""
        mock_recovery_service = Mock()
        mock_recovery_service_class.return_value = mock_recovery_service

        def failing_generator():
            yield Mock(id='item_1', permalink='/r/test/1')
            raise prawcore.exceptions.NotFound(Mock())

        result = list(safe_fetch_items_one_by_one(
            failing_generator(),
            'comment',
            recovery_enabled=True
        ))

        self.assertEqual([item.id for item in result], ['item_1'])
        mock_recovery_service.attempt_recovery.assert_not_called()

    def test_skip_on_forbidden(self):
        """Test skipping forbidden items."""
        def failing_generator():
            yield Mock(id='item_1')
            raise prawcore.exceptions.Forbidden(Mock())

        result = list(safe_fetch_items_one_by_one(
            failing_generator(),
            'test',
            recovery_enabled=False
        ))

        # Should only get the first item, second is skipped
        self.assertEqual(len(result), 1)


class TestIntegrationScenarios(unittest.TestCase):
    """Integration tests for real-world scenarios."""

    @patch('utils.praw_helpers.ContentRecoveryService')
    def test_mixed_success_and_deleted_items(self, mock_recovery_service_class):
        """Test handling of mixed successful and deleted items."""
        # A recovery service must not receive a URL for a previously saved item.
        mock_recovery_service = Mock()
        mock_recovery_service_class.return_value = mock_recovery_service

        # Create generator with successful and failing items
        successful_count = 0
        failed_count = 0

        def mixed_generator():
            nonlocal successful_count, failed_count

            for i in range(3):
                successful_count += 1
                yield Mock(id=f'success_{i}', permalink=f'/r/test/{i}')

            # Simulate deleted item
            failed_count += 1
            raise prawcore.exceptions.NotFound(Mock())

        result = list(safe_fetch_items_one_by_one(
            mixed_generator(),
            'comment',
            recovery_enabled=True
        ))

        self.assertEqual(len(result), 3)
        mock_recovery_service.attempt_recovery.assert_not_called()


if __name__ == '__main__':
    unittest.main()
