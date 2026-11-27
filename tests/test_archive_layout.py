"""Regression tests for the archive layout actually used by Reddit Stash.

The old version of this test imported an ordered/date-partitioned path helper
that no longer exists in the application. The scraper's supported on-disk
layout is one subreddit directory with category-prefixed Markdown filenames.
"""
from pathlib import Path
import tempfile
import unittest

from utils.path_security import create_reddit_file_path


class ArchiveLayoutTests(unittest.TestCase):
    def test_submission_path_is_nested_under_sanitized_subreddit(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = create_reddit_file_path(tmp, "pics", "POST", "abc123")
            self.assertTrue(result.is_safe)
            self.assertEqual(
                Path(result.safe_path),
                Path(tmp) / "pics" / "POST_abc123.md",
            )

    def test_comment_path_uses_comment_category_prefix(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = create_reddit_file_path(tmp, "AskReddit", "COMMENT", "def456")
            self.assertTrue(result.is_safe)
            self.assertEqual(
                Path(result.safe_path),
                Path(tmp) / "AskReddit" / "COMMENT_def456.md",
            )

    def test_unsafe_subreddit_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = create_reddit_file_path(tmp, "../outside", "POST", "abc123")
            self.assertFalse(result.is_safe)


if __name__ == "__main__":
    unittest.main()
