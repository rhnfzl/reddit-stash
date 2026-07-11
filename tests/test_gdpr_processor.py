"""Regression tests for GDPR export processing."""

import csv
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from utils.gdpr_processor import process_gdpr_export


class TestGdprExportProcessing(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.save_directory = self.temp_dir.name
        self.gdpr_directory = Path(self.save_directory) / 'gdpr_data'
        self.gdpr_directory.mkdir()

    def tearDown(self):
        self.temp_dir.cleanup()

    def _write_export(self, filename, item_id):
        with (self.gdpr_directory / filename).open('w', newline='', encoding='utf-8') as export_file:
            csv.DictWriter(export_file, fieldnames=['id']).writeheader()
            csv.DictWriter(export_file, fieldnames=['id']).writerow({'id': item_id})

    def _reddit_item(self, item_id, *, is_submission):
        item = Mock()
        item.id = item_id
        item.subreddit.display_name = 'test'
        if is_submission:
            item.is_self = True
            item.selftext = 'body'
        else:
            item.body = 'body'
        return item

    def _process(self, reddit):
        return process_gdpr_export(reddit, self.save_directory, set(), set(), {})

    def test_saved_submission_tuple_result_is_unpacked(self):
        self._write_export('saved_posts.csv', 'post123')
        reddit = Mock()
        reddit.submission.return_value = self._reddit_item('post123', is_submission=True)

        with patch('utils.gdpr_processor.save_to_file', return_value=(False, 0)):
            with patch('utils.gdpr_processor.os.path.getsize', return_value=42):
                with patch('utils.gdpr_processor.dynamic_sleep'):
                    processed, skipped, total_size = self._process(reddit)

        self.assertEqual((processed, skipped, total_size), (1, 0, 42))

    def test_saved_comment_tuple_result_is_unpacked(self):
        self._write_export('saved_comments.csv', 'comment123')
        reddit = Mock()
        reddit.comment.return_value = self._reddit_item('comment123', is_submission=False)

        with patch('utils.gdpr_processor.save_to_file', return_value=(False, 0)):
            with patch('utils.gdpr_processor.os.path.getsize', return_value=42):
                with patch('utils.gdpr_processor.dynamic_sleep'):
                    processed, skipped, total_size = self._process(reddit)

        self.assertEqual((processed, skipped, total_size), (1, 0, 42))

    def test_already_saved_submission_is_skipped(self):
        self._write_export('saved_posts.csv', 'post123')
        reddit = Mock()
        reddit.submission.return_value = self._reddit_item('post123', is_submission=True)

        with patch('utils.gdpr_processor.save_to_file', return_value=(True, 0)):
            with patch('utils.gdpr_processor.dynamic_sleep') as sleep:
                processed, skipped, total_size = self._process(reddit)

        self.assertEqual((processed, skipped, total_size), (0, 1, 0))
        sleep.assert_not_called()


if __name__ == '__main__':
    unittest.main()
