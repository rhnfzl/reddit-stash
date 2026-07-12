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

    def _write_export_rows(self, filename, item_ids):
        with (self.gdpr_directory / filename).open('w', newline='', encoding='utf-8') as export_file:
            writer = csv.DictWriter(export_file, fieldnames=['id'])
            writer.writeheader()
            writer.writerows({'id': item_id} for item_id in item_ids)

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
        reddit.info.return_value = [self._reddit_item('post123', is_submission=True)]

        with patch('utils.gdpr_processor.save_to_file', return_value=(False, 0)):
            with patch('utils.gdpr_processor.os.path.getsize', return_value=42):
                with patch('utils.gdpr_processor.dynamic_sleep'):
                    processed, skipped, total_size = self._process(reddit)

        self.assertEqual((processed, skipped, total_size), (1, 0, 42))
        reddit.submission.assert_not_called()

    def test_saved_comment_tuple_result_is_unpacked(self):
        self._write_export('saved_comments.csv', 'comment123')
        reddit = Mock()
        reddit.info.return_value = [self._reddit_item('comment123', is_submission=False)]

        with patch('utils.gdpr_processor.save_to_file', return_value=(False, 0)):
            with patch('utils.gdpr_processor.os.path.getsize', return_value=42):
                with patch('utils.gdpr_processor.dynamic_sleep'):
                    processed, skipped, total_size = self._process(reddit)

        self.assertEqual((processed, skipped, total_size), (1, 0, 42))
        reddit.comment.assert_not_called()

    def test_already_saved_submission_is_skipped(self):
        self._write_export('saved_posts.csv', 'post123')
        reddit = Mock()
        reddit.info.return_value = [self._reddit_item('post123', is_submission=True)]

        with patch('utils.gdpr_processor.save_to_file', return_value=(True, 0)):
            with patch('utils.gdpr_processor.dynamic_sleep') as sleep:
                processed, skipped, total_size = self._process(reddit)

        self.assertEqual((processed, skipped, total_size), (0, 1, 0))
        sleep.assert_not_called()

    def test_saved_posts_are_fetched_in_one_info_batch(self):
        self._write_export_rows('saved_posts.csv', ['post123', 'post456'])
        reddit = Mock()
        reddit.info.return_value = [
            self._reddit_item('post123', is_submission=True),
            self._reddit_item('post456', is_submission=True),
        ]

        with patch('utils.gdpr_processor.save_to_file', return_value=(False, 0)):
            with patch('utils.gdpr_processor.os.path.getsize', return_value=42):
                with patch('utils.gdpr_processor.dynamic_sleep'):
                    processed, skipped, total_size = self._process(reddit)

        self.assertEqual((processed, skipped, total_size), (2, 0, 84))
        reddit.info.assert_called_once_with(fullnames=['t3_post123', 't3_post456'])
        reddit.submission.assert_not_called()

    def test_archive_id_treats_missing_values_as_empty(self):
        # With pandas removed, empty CSV cells arrive as '' (not NaN); _archive_id
        # must still normalize missing ids to '' and strip the t1_/t3_ prefixes.
        from utils.gdpr_processor import _archive_id
        self.assertEqual(_archive_id(''), '')
        self.assertEqual(_archive_id(None), '')
        self.assertEqual(_archive_id('   '), '')
        self.assertEqual(_archive_id('t3_abc123'), 'abc123')
        self.assertEqual(_archive_id('t1_def456'), 'def456')
        self.assertEqual(_archive_id('ghi789'), 'ghi789')

    def test_read_csv_rows_returns_empty_string_for_missing_cells(self):
        # A row with an empty id must read back as '' rather than raising or
        # producing a NaN float, so downstream null checks keep working.
        from utils.gdpr_processor import _archive_id, _read_csv_rows
        path = self.gdpr_directory / 'saved_posts.csv'
        with path.open('w', newline='', encoding='utf-8') as export_file:
            export_file.write('id,permalink\n')
            export_file.write('post123,/r/test/comments/post123/\n')
            export_file.write(',\n')
        rows = _read_csv_rows(str(path))
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]['id'], 'post123')
        self.assertEqual(rows[1]['id'], '')
        self.assertEqual(_archive_id(rows[1]['id']), '')


if __name__ == '__main__':
    unittest.main()
