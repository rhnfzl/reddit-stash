"""Regression tests for GDPR export processing."""

import configparser
import csv
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from utils.content_recovery.recovery_config import RecoveryConfig
from utils.file_operations import RunStats, SaveContext, SaveStatus
from utils.gdpr_processor import _COMMENTS, _POSTS, _UPVOTED_COMMENTS, process_gdpr_export


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
        return process_gdpr_export(reddit, SaveContext(self.save_directory, set(), {}, set()))

    def test_saved_submission_tuple_result_is_unpacked(self):
        self._write_export('saved_posts.csv', 'post123')
        reddit = Mock()
        reddit.info.return_value = [self._reddit_item('post123', is_submission=True)]

        with patch('utils.file_operations.save_to_file', return_value=(SaveStatus.SAVED, 0)):
            with patch('utils.file_operations.os.path.getsize', return_value=42):
                with patch('utils.file_operations.dynamic_sleep'):
                    stats = self._process(reddit)

        self.assertEqual((stats.processed, stats.skipped, stats.total_size), (1, 0, 42))
        reddit.submission.assert_not_called()

    def test_saved_comment_tuple_result_is_unpacked(self):
        self._write_export('saved_comments.csv', 'comment123')
        reddit = Mock()
        reddit.info.return_value = [self._reddit_item('comment123', is_submission=False)]

        with patch('utils.file_operations.save_to_file', return_value=(SaveStatus.SAVED, 0)):
            with patch('utils.file_operations.os.path.getsize', return_value=42):
                with patch('utils.file_operations.dynamic_sleep'):
                    stats = self._process(reddit)

        self.assertEqual((stats.processed, stats.skipped, stats.total_size), (1, 0, 42))
        reddit.comment.assert_not_called()

    def test_already_saved_submission_is_skipped(self):
        self._write_export('saved_posts.csv', 'post123')
        reddit = Mock()
        reddit.info.return_value = [self._reddit_item('post123', is_submission=True)]

        with patch('utils.file_operations.save_to_file', return_value=(SaveStatus.SKIPPED, 0)):
            with patch('utils.file_operations.dynamic_sleep') as sleep:
                stats = self._process(reddit)

        self.assertEqual((stats.processed, stats.skipped, stats.total_size), (0, 1, 0))
        sleep.assert_not_called()

    def test_failed_submission_counts_as_failed_not_processed(self):
        self._write_export('saved_posts.csv', 'post123')
        reddit = Mock()
        reddit.info.return_value = [self._reddit_item('post123', is_submission=True)]

        with patch('utils.file_operations.save_to_file', return_value=(SaveStatus.FAILED, 0)):
            with patch('utils.file_operations.dynamic_sleep'):
                result = self._process(reddit)

        self.assertEqual(result, RunStats(failed=1))

    def test_saved_posts_are_fetched_in_one_info_batch(self):
        self._write_export_rows('saved_posts.csv', ['post123', 'post456'])
        reddit = Mock()
        reddit.info.return_value = [
            self._reddit_item('post123', is_submission=True),
            self._reddit_item('post456', is_submission=True),
        ]

        with patch('utils.file_operations.save_to_file', return_value=(SaveStatus.SAVED, 0)):
            with patch('utils.file_operations.os.path.getsize', return_value=42):
                with patch('utils.file_operations.dynamic_sleep'):
                    stats = self._process(reddit)

        self.assertEqual((stats.processed, stats.skipped, stats.total_size), (2, 0, 84))
        reddit.info.assert_called_once_with(fullnames=['t3_post123', 't3_post456'])
        reddit.submission.assert_not_called()

    def test_api_mode_keeps_logging_under_the_post_category(self):
        # Existing file_log.json files rely on this key, despite the GDPR_ file name.
        self._write_export_rows('saved_posts.csv', ['post123'])
        self._write_export_rows('saved_comments.csv', ['comment123'])
        reddit = Mock()
        reddit.info.side_effect = [
            [self._reddit_item('post123', is_submission=True)],
            [self._reddit_item('comment123', is_submission=False)],
        ]
        file_log = {}

        with patch('utils.gdpr_processor._POSTS', replace(_POSTS, save_function=lambda item, f: 0)), \
                patch('utils.gdpr_processor._COMMENTS', replace(_COMMENTS, save_function=lambda item, f: 0)), \
                patch('utils.file_operations.dynamic_sleep'):
            stats = process_gdpr_export(reddit, SaveContext(self.save_directory, set(), file_log, set()))

        self.assertEqual(stats.processed, 2)
        self.assertEqual(set(file_log), {'post123-test-Mock-POST', 'comment123-test-Mock-POST'})
        self.assertTrue((Path(self.save_directory) / 'test' / 'GDPR_POST_post123.md').exists())
        self.assertTrue((Path(self.save_directory) / 'test' / 'GDPR_COMMENT_comment123.md').exists())

    def _write_votes(self, votes):
        with (self.gdpr_directory / 'comment_votes.csv').open('w', newline='', encoding='utf-8') as export_file:
            writer = csv.DictWriter(export_file, fieldnames=['id', 'permalink', 'direction'])
            writer.writeheader()
            writer.writerows({'id': i, 'permalink': f'/r/test/comments/x/y/{i}/', 'direction': d} for i, d in votes)

    def test_upvoted_comments_are_saved_like_the_api_upvoted_listing(self):
        self._write_votes([('up1', 'up'), ('down1', 'down'), ('none1', 'none')])
        reddit = Mock()
        reddit.info.return_value = [self._reddit_item('up1', is_submission=False)]
        file_log = {}

        with patch('utils.gdpr_processor._UPVOTED_COMMENTS', replace(_UPVOTED_COMMENTS, save_function=lambda item, f: 0)), \
                patch('utils.file_operations.dynamic_sleep'):
            stats = process_gdpr_export(reddit, SaveContext(self.save_directory, set(), file_log, set()))

        self.assertEqual((stats.processed, stats.failed), (1, 0))
        self.assertEqual(reddit.info.call_args.kwargs['fullnames'], ['t1_up1'])
        self.assertEqual(set(file_log), {'up1-test-Mock-UPVOTE_COMMENT'})
        self.assertTrue((Path(self.save_directory) / 'test' / 'UPVOTE_COMMENT_up1.md').exists())

    def test_csv_only_mode_reads_full_url_permalinks(self):
        with (self.gdpr_directory / 'saved_posts.csv').open('w', newline='', encoding='utf-8') as export_file:
            writer = csv.DictWriter(export_file, fieldnames=['id', 'permalink'])
            writer.writeheader()
            writer.writerow({'id': 'abc', 'permalink': 'https://www.reddit.com/r/Python/comments/abc/title/'})
        config = RecoveryConfig(timeout_seconds=7, use_arctic_shift=False, use_pullpush=False)
        with patch('utils.gdpr_processor.load_recovery_config', return_value=config):
            self._process(None)

        export = Path(self.save_directory) / 'r_Python' / 'GDPR_POST_abc.md'
        self.assertIn('[https://www.reddit.com/r/Python/comments/abc/title/]', export.read_text(encoding='utf-8'))

    def test_csv_only_mode_saves_only_upvoted_comments(self):
        self._write_votes([('up1', 'up'), ('down1', 'down')])
        config = RecoveryConfig(timeout_seconds=7, use_arctic_shift=False, use_pullpush=False)
        ctx = SaveContext(self.save_directory, set(), {}, set())
        with patch('utils.gdpr_processor.load_recovery_config', return_value=config):
            stats = process_gdpr_export(None, ctx)

        self.assertEqual(stats.processed, 1)
        self.assertEqual(set(ctx.file_log), {'UPVOTE_COMMENT_up1'})

    def _run_csv_only_with_recovery(self, recovery):
        self._write_export_rows('saved_posts.csv', ['post123'])
        config = RecoveryConfig(timeout_seconds=7, **recovery)
        with patch('utils.gdpr_processor.load_recovery_config', return_value=config), \
                patch('utils.gdpr_processor.ArcticShiftClient') as arctic, \
                patch('utils.gdpr_processor.PullPushProvider') as pullpush:
            arctic.return_value.fetch_posts.return_value = {}
            pullpush.return_value.fetch_posts.return_value = {}
            stats = self._process(None)
        return stats, arctic, pullpush

    def test_csv_only_mode_respects_disabled_recovery_providers(self):
        stats, arctic, pullpush = self._run_csv_only_with_recovery(
            {'use_arctic_shift': False, 'use_pullpush': False})

        arctic.assert_not_called()
        pullpush.assert_not_called()
        self.assertEqual(stats.processed, 1)  # the link-only export is still written

    def test_csv_only_mode_uses_enabled_providers_with_configured_timeout(self):
        _, arctic, pullpush = self._run_csv_only_with_recovery(
            {'use_arctic_shift': True, 'use_pullpush': True})

        arctic.assert_called_once_with(timeout=7)
        pullpush.assert_called_once_with(7)
        pullpush.return_value.fetch_posts.assert_called_once_with(['post123'])

    def test_csv_only_mode_gates_each_provider_on_its_own_flag(self):
        _, arctic, pullpush = self._run_csv_only_with_recovery(
            {'use_arctic_shift': True, 'use_pullpush': False})
        arctic.assert_called_once_with(timeout=7)
        pullpush.assert_not_called()

        _, arctic, pullpush = self._run_csv_only_with_recovery(
            {'use_arctic_shift': False, 'use_pullpush': True})
        arctic.assert_not_called()
        pullpush.assert_called_once_with(7)

    def test_csv_only_mode_names_the_section_of_a_malformed_recovery_value(self):
        parser = configparser.ConfigParser()
        parser.read_string('[Recovery]\ntimeout_seconds = abc\n')
        with patch('utils.content_recovery.recovery_config.load_settings', return_value=parser), \
                self.assertRaisesRegex(ValueError, r'\[Recovery\].*abc'):
            self._process(None)

    def test_csv_only_unsafe_path_counts_as_failed(self):
        unsafe = Mock(is_safe=False, issues=['traversal'])
        with self.assertLogs('utils.gdpr_processor', 'ERROR'), \
                patch('utils.gdpr_processor.create_reddit_file_path', return_value=unsafe):
            stats, _, _ = self._run_csv_only_with_recovery({'use_arctic_shift': False, 'use_pullpush': False})

        self.assertEqual((stats.failed, stats.skipped, stats.processed), (1, 0, 0))

    def test_csv_only_row_already_in_the_log_is_not_rewritten(self):
        self._write_export_rows('saved_posts.csv', ['post123'])
        config = RecoveryConfig(timeout_seconds=7, use_arctic_shift=False, use_pullpush=False)
        ctx = SaveContext(self.save_directory, set(), {}, set())
        with patch('utils.gdpr_processor.load_recovery_config', return_value=config):
            process_gdpr_export(None, ctx)
            export = next(Path(self.save_directory).rglob('GDPR_POST_post123*'))
            export.write_text('kept', encoding='utf-8')

            stats = process_gdpr_export(None, ctx)

        self.assertEqual((stats.skipped, stats.processed), (1, 0))
        self.assertEqual(export.read_text(encoding='utf-8'), 'kept')

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
