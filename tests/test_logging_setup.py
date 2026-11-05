"""Entry points must make the project's log messages visible, and report errors through them."""

import csv
import io
import logging
import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

import clean_items
import reddit_stash
import storage_utils
from utils.file_operations import SaveContext, SaveStatus, save_to_file
from utils.gdpr_processor import process_gdpr_export

_PROBE = """
import logging
from utils.log_utils import configure_logging
from utils.time_utilities import dynamic_sleep
configure_logging()
dynamic_sleep(0)
logging.getLogger('utils.probe').info('project info')
logging.getLogger('dropbox').info('library info')
logging.getLogger('dropbox').warning('library warning')
"""


class TestConfigureLogging(unittest.TestCase):
    def test_project_info_is_shown_and_library_info_is_not(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        result = subprocess.run(
            [sys.executable, '-c', _PROBE], cwd=root, capture_output=True, text=True, check=True
        )
        self.assertIn('project info', result.stderr)
        self.assertIn('library warning', result.stderr)
        self.assertNotIn('library info', result.stderr)
        self.assertNotIn('Sleeping', result.stderr)

    def test_each_entry_point_configures_logging(self):
        for module, argv in ((clean_items, ['clean_items.py', '--help']), (storage_utils, ['storage_utils.py', '--help'])):
            with self.subTest(module=module.__name__), \
                    patch.object(module, 'configure_logging') as configure, \
                    patch.object(sys, 'argv', argv), \
                    patch('sys.stdout', io.StringIO()), \
                    self.assertRaises(SystemExit):
                module.main()
            configure.assert_called_once_with()

    def test_reddit_stash_configures_logging_before_validating(self):
        with patch.object(reddit_stash, 'configure_logging') as configure, \
                patch.object(reddit_stash, 'validate_config_or_exit', side_effect=SystemExit(1)), \
                self.assertRaises(SystemExit):
            reddit_stash.main()
        configure.assert_called_once_with()


class TestErrorsReachTheLog(unittest.TestCase):
    def test_gdpr_row_failure_is_logged_as_an_error(self):
        with tempfile.TemporaryDirectory() as save_dir:
            gdpr_dir = os.path.join(save_dir, 'gdpr_data')
            os.makedirs(gdpr_dir)
            with open(os.path.join(gdpr_dir, 'saved_posts.csv'), 'w', newline='', encoding='utf-8') as handle:
                writer = csv.DictWriter(handle, fieldnames=['id', 'permalink'])
                writer.writeheader()
                writer.writerow({'id': 'abc', 'permalink': '/r/test/comments/abc/x/'})
            ctx = SaveContext(save_dir, set(), {}, set())
            reddit = Mock()
            reddit.info.return_value = []
            reddit.submission.side_effect = RuntimeError('boom')

            with self.assertLogs('utils.gdpr_processor', level='ERROR') as logs:
                stats = process_gdpr_export(reddit, ctx)

        self.assertEqual(stats.failed, 1)
        self.assertIn('abc', logs.output[0])

    def test_partial_file_that_cannot_be_removed_is_reported(self):
        with tempfile.TemporaryDirectory() as save_dir:
            file_path = os.path.join(save_dir, 'r_test', 'POST_abc.md')
            content = Mock(id='abc')
            content.subreddit.display_name = 'test'
            ctx = SaveContext(save_dir, set(), {}, set())

            def fail_mid_write(item, f):
                f.write('partial')
                raise RuntimeError('save failed')

            with patch('utils.file_operations.os.remove', side_effect=OSError('locked')), \
                    self.assertLogs('utils.file_operations', level='WARNING') as logs:
                status, _ = save_to_file(content, file_path, fail_mid_write, ctx)

        self.assertIs(status, SaveStatus.FAILED)
        self.assertTrue(any('partial file' in line for line in logs.output))


class TestItemUrlsStayOutOfInfoLogs(unittest.TestCase):
    def test_constructed_dash_url_is_not_logged_at_info(self):
        from utils.save_utils import _get_video_download_url

        submission = Mock(id='abc', url='https://v.redd.it/abc', media=None, secure_media=None)
        logger = logging.getLogger('utils.save_utils')
        with patch.object(logger, 'info') as info:
            self.assertEqual(_get_video_download_url(submission), 'https://v.redd.it/abc/DASH_720.mp4')
        self.assertFalse(any('v.redd.it/abc/' in str(call) for call in info.call_args_list))

    def test_item_ids_are_not_logged_at_info(self):
        from utils.save_utils import _get_video_download_url, _write_link_post_body

        submission = Mock(id='abc', url='https://v.redd.it/abc', media=None, secure_media=None)
        with tempfile.NamedTemporaryFile('w') as handle, \
                patch('utils.save_utils.get_media_config'), \
                patch('utils.save_utils._save_submission_media', side_effect=RuntimeError('boom')), \
                self.assertNoLogs('utils.save_utils', level='INFO'):
            _get_video_download_url(submission)
            submission.selftext = ''
            _write_link_post_body(submission, handle, context_mode=True)


if __name__ == '__main__':
    unittest.main()
