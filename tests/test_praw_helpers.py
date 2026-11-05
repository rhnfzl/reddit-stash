"""Tests for reading a PRAW listing that Reddit may cut short, and for the comment-load retry."""

import unittest
from unittest.mock import Mock, PropertyMock, patch

import prawcore

from utils.constants import COMMENT_FETCH_MAX_ATTEMPTS
from utils.praw_helpers import fetch_until_unavailable, lazy_load_comments


class RaisingIterable:
    """An iterable that fails when the consumer starts iteration."""

    def __init__(self, error):
        self.error = error

    def __iter__(self):
        raise self.error


def _fails_after(items, error):
    yield from items
    raise error


class TestFetchUntilUnavailable(unittest.TestCase):
    def test_complete_listing_is_not_marked_as_stopped(self):
        self.assertEqual(fetch_until_unavailable(iter(['a', 'b', 'c']), 'test'), (['a', 'b', 'c'], False))

    def test_failure_at_start_returns_nothing_and_marks_stopped(self):
        listing = RaisingIterable(prawcore.exceptions.NotFound(Mock()))
        self.assertEqual(fetch_until_unavailable(listing, 'test'), ([], True))

    def test_not_found_keeps_earlier_items_and_marks_stopped(self):
        listing = _fails_after(['a', 'b'], prawcore.exceptions.NotFound(Mock()))
        self.assertEqual(fetch_until_unavailable(listing, 'comment'), (['a', 'b'], True))

    def test_forbidden_keeps_earlier_items_and_marks_stopped(self):
        listing = _fails_after(['a'], prawcore.exceptions.Forbidden(Mock()))
        self.assertEqual(fetch_until_unavailable(listing, 'test'), (['a'], True))

    def test_other_errors_are_not_swallowed(self):
        listing = _fails_after(['a'], RuntimeError('boom'))
        with self.assertRaises(RuntimeError):
            fetch_until_unavailable(listing, 'test')


class TestLazyLoadComments(unittest.TestCase):
    def setUp(self):
        patcher = patch('utils.time_utilities.time.sleep')
        self.sleep = patcher.start()
        self.addCleanup(patcher.stop)

    def test_retries_rate_limit_then_yields_once(self):
        rate_limited = prawcore.exceptions.TooManyRequests(Mock(status_code=429, headers={}))
        submission = Mock()
        type(submission).comments = PropertyMock(side_effect=[rate_limited, ['a', 'b']])

        self.assertEqual(list(lazy_load_comments(submission)), ['a', 'b'])
        self.sleep.assert_called_once()

    def test_gives_up_on_endless_rate_limiting(self):
        rate_limited = prawcore.exceptions.TooManyRequests(Mock(status_code=429, headers={}))
        submission = Mock()
        type(submission).comments = PropertyMock(side_effect=rate_limited)

        with self.assertRaises(prawcore.exceptions.TooManyRequests):
            list(lazy_load_comments(submission))
        self.assertEqual(self.sleep.call_count, COMMENT_FETCH_MAX_ATTEMPTS - 1)


if __name__ == '__main__':
    unittest.main()
