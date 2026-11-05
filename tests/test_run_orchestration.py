"""save_user_activity and main, driven through the real settings loader and listing fetch."""

import contextlib
import io
import os
import sqlite3
import unittest
from unittest.mock import Mock, patch

import prawcore
from PIL import Image
from praw.models import Submission

import reddit_stash
from tests.helpers import SettingsFileTestCase, praw_comment, praw_submission
from tests.media_helpers import FakeResponse
from utils.constants import MAX_ITEM_FAILURES
from utils.feature_flags import get_media_config
from utils.file_operations import RunStats, activity_save_context, save_user_activity
from utils.log_utils import load_file_log, save_file_log
from utils.media_services import download_manager as manager_module
from utils.media_services.base_downloader import BaseHTTPDownloader
from utils.media_services.retry_queue import SQLiteRetryQueue

MIB = 1024 * 1024
RETRYABLE = {'status': 'failed', 'attempts': 1}
GIVEN_UP = {'status': 'failed', 'attempts': MAX_ITEM_FAILURES}


def _write_body(item, f):
    if item.id == 'bad':
        raise RuntimeError('save failed')
    f.write('x' * 10)
    return 0


def _account(**listings):
    """A Reddit client whose user answers the way PRAW listings do."""
    reddit = Mock()
    user = reddit.user.me.return_value
    user.submissions.new.return_value = listings.get('submissions', [])
    user.comments.new.return_value = listings.get('comments', [])
    user.saved.side_effect = lambda limit: iter(listings.get('saved', []))
    user.upvoted.side_effect = lambda limit: iter(listings.get('upvoted', []))
    return reddit, user


class TestSavePipeline(SettingsFileTestCase):
    """From the listing to the files on disk, with only the network replaced."""

    SETTINGS = (
        '[Settings]\nsave_type = SAVED\ncheck_type = LOG\n'
        '[Media]\ndownload_enabled = true\n'
        '[Recovery]\nuse_wayback_machine = false\nuse_arctic_shift = false\nuse_pullpush = false\n'
    )
    URL = 'https://i.redd.it/pic.png'

    def setUp(self):
        super().setUp()
        self.save_dir = os.path.realpath(os.path.join(self.root, 'saves'))
        os.mkdir(self.save_dir)
        get_media_config.cache_clear()
        self.addCleanup(get_media_config.cache_clear)
        self.queue = SQLiteRetryQueue(os.path.join(self.root, 'retry.db'))
        for patcher in (
            patch.object(manager_module, '_media_manager', None),
            patch.object(manager_module, 'get_retry_queue', return_value=self.queue),
            patch('utils.content_recovery.recovery_service.RecoveryCacheManager'),
            patch('utils.media_services.url_security.socket.getaddrinfo',
                  return_value=[(2, 1, 6, '', ('93.184.216.34', 0))]),
            patch.object(BaseHTTPDownloader, '_respect_rate_limit'),
            patch('utils.file_operations.sleep_for_item'),
            patch.object(Submission, 'comments', []),  # PRAW would fetch these from Reddit
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _link_post(self):
        post = praw_submission('abc123')
        for name, value in {
            'title': 'A picture', 'created_utc': 1700000000, 'author': '[deleted]', 'link_flair_text': None,
            'num_comments': 0, 'permalink': '/r/python/comments/abc123/a_picture/', 'score': 7,
            'is_self': False, 'selftext': '', 'url': self.URL, 'is_gallery': False,
        }.items():
            setattr(post, name, value)
        return post

    def _save(self, response):
        file_log = {}
        with patch.object(BaseHTTPDownloader, '_get_with_safe_redirects', return_value=(response, self.URL)), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            ctx = activity_save_context(self.save_dir, file_log)
            stats = save_user_activity(_account(saved=[self._link_post()])[0], ctx)
        with open(os.path.join(self.save_dir, 'python', 'SAVED_POST_abc123.md'), encoding='utf-8') as f:
            return stats, f.read(), file_log

    def test_saved_link_post_writes_markdown_media_and_log(self):
        png = io.BytesIO()
        Image.effect_noise((32, 32), 50).save(png, 'PNG')

        stats, markdown, file_log = self._save(FakeResponse(png.getvalue(), content_type='image/png'))

        media_path = os.path.join(self.save_dir, 'python', 'abc123.png')
        self.assertEqual((stats.processed, stats.failed), (1, 0))
        self.assertEqual(stats.media_size, len(png.getvalue()))
        self.assertIn(f'![Image]({media_path})', markdown)
        self.assertIn(f'**Original Image URL:** [Link]({self.URL})', markdown)
        with open(media_path, 'rb') as saved:
            self.assertEqual(saved.read(), png.getvalue())
        self.assertEqual(file_log['abc123-python-Submission-SAVED_POST']['type'], 'Submission')

    def test_failed_media_keeps_the_post_links_the_url_and_queues_a_retry(self):
        stats, markdown, file_log = self._save(FakeResponse(b'<html></html>', content_type='text/html'))

        self.assertEqual((stats.processed, stats.failed, stats.media_size), (1, 0, 0))
        self.assertIn(f'![Image]({self.URL})', markdown)
        self.assertIn('abc123-python-Submission-SAVED_POST', file_log)
        self.assertEqual(os.listdir(os.path.join(self.save_dir, 'python')), ['SAVED_POST_abc123.md'])
        with sqlite3.connect(os.path.join(self.root, 'retry.db')) as db:
            self.assertEqual(db.execute('SELECT url FROM retry_queue').fetchall(), [(self.URL,)])


class TestSaveUserActivity(SettingsFileTestCase):
    def setUp(self):
        super().setUp()
        self.save_dir = os.path.realpath(os.path.join(self.root, 'saves'))
        os.mkdir(self.save_dir)
        for target, replacement in (
            ('save_submission', _write_body),
            ('save_comment_and_context', _write_body),
            ('sleep_for_item', Mock()),
            ('_clone_reddit', lambda reddit: reddit),
        ):
            patcher = patch(f'utils.file_operations.{target}', replacement)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _run(self, reddit, save_type='SAVED', check_type='LOG', file_log=None):
        self.write_settings(f'[Settings]\nsave_type = {save_type}\ncheck_type = {check_type}\n')
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            ctx = activity_save_context(self.save_dir, {} if file_log is None else file_log)
            return save_user_activity(reddit, ctx)

    def test_each_save_type_asks_only_for_its_listings(self):
        modes = {
            'ALL': {'submissions', 'comments', 'saved', 'upvoted'},
            'ACTIVITY': {'submissions', 'comments'},
            'SAVED': {'saved'},
            'UPVOTED': {'upvoted'},
            'saved': {'saved'},  # the setting is not case sensitive
        }
        for save_type, expected in modes.items():
            with self.subTest(save_type=save_type):
                reddit, user = _account()
                self._run(reddit, save_type)

                endpoints = {
                    'submissions': user.submissions.new, 'comments': user.comments.new,
                    'saved': user.saved, 'upvoted': user.upvoted,
                }
                for name, endpoint in endpoints.items():
                    self.assertEqual(endpoint.call_args_list != [], name in expected, name)
                    if name in expected:
                        endpoint.assert_called_once_with(limit=1000)

    def test_log_mode_trusts_the_log_and_retries_failures(self):
        os.mkdir(os.path.join(self.save_dir, 'python'))
        open(os.path.join(self.save_dir, 'python', 'SAVED_POST_disk.md'), 'w').close()
        file_log = {
            'logged-python-Submission-SAVED_POST': {'type': 'Submission', 'file_path': 'python/SAVED_POST_logged.md'},
            'retry-python-Submission-SAVED_POST': dict(RETRYABLE),
            'dead-python-Submission-SAVED_POST': dict(GIVEN_UP),
        }
        items = [praw_submission(item_id) for item_id in ('logged', 'retry', 'dead', 'disk')]

        stats = self._run(_account(saved=items)[0], check_type='LOG', file_log=file_log)

        self.assertEqual((stats.processed, stats.skipped), (2, 2))  # retry and disk are saved
        self.assertNotIn('status', file_log['retry-python-Submission-SAVED_POST'])

    def test_dir_mode_trusts_the_files_and_still_skips_given_up_items(self):
        os.mkdir(os.path.join(self.save_dir, 'python'))
        open(os.path.join(self.save_dir, 'python', 'SAVED_POST_disk.md'), 'w').close()
        file_log = {
            'logged-python-Submission-SAVED_POST': {'type': 'Submission', 'file_path': 'python/SAVED_POST_logged.md'},
            'dead-python-Submission-SAVED_POST': dict(GIVEN_UP),
        }
        items = [praw_submission(item_id) for item_id in ('logged', 'dead', 'disk')]

        stats = self._run(_account(saved=items)[0], check_type='DIR', file_log=file_log)

        self.assertEqual((stats.processed, stats.skipped), (1, 2))  # logged has no file, so it is saved again
        self.assertTrue(os.path.exists(os.path.join(self.save_dir, 'python', 'SAVED_POST_logged.md')))

    def test_unknown_modes_stop_before_any_fetch(self):
        for settings in ({'save_type': 'WAT'}, {'check_type': 'WAT'}):
            with self.subTest(settings=settings):
                reddit, _ = _account()
                with self.assertRaisesRegex(ValueError, 'WAT'):
                    self._run(reddit, **settings)
                reddit.user.me.assert_not_called()

    def test_all_mode_merges_every_listing_into_the_log(self):
        def saved_until_404(limit):
            yield praw_submission('sav')
            raise prawcore.exceptions.NotFound(Mock(status_code=404, headers={}))

        reddit, user = _account(
            submissions=[praw_submission('sub')],
            comments=[praw_comment('com')],
            upvoted=[praw_submission('old'), praw_submission('bad')],
        )
        user.saved.side_effect = saved_until_404
        file_log = {'old-python-Submission-UPVOTE_POST': {'type': 'Submission', 'file_path': 'x'}}

        stats = self._run(reddit, 'ALL', file_log=file_log)

        self.assertEqual(
            (stats.processed, stats.skipped, stats.failed, stats.total_size),
            (3, 1, 1, 30),
        )
        self.assertEqual(stats.truncated_listings, ('saved',))
        self.assertIn('sav-python-Submission-SAVED_POST', file_log)
        self.assertEqual(file_log['bad-python-Submission-UPVOTE_POST']['status'], 'failed')


class TestMainRun(SettingsFileTestCase):
    def setUp(self):
        super().setUp()
        self.write_settings(
            f'[Settings]\nsave_directory = {self.root}\nprocess_api = true\n'
            'process_gdpr = true\nunsave_after_download = true\n'
        )
        for target in ('validate_configuration', 'get_feature_summary', 'create_reddit_client',
                       'save_user_activity', 'process_gdpr_export', 'retry_pending_media'):
            patcher = patch(f'reddit_stash.{target}')
            setattr(self, target, patcher.start())
            self.addCleanup(patcher.stop)
        self.validate_configuration.return_value = {'warnings': []}
        self.get_feature_summary.return_value = ''

        def api_run(reddit, ctx, unsave=False):
            ctx.file_log['api-key'] = {'type': 'Submission', 'file_path': 'x'}
            return RunStats(processed=2, skipped=1, failed=1, total_size=MIB, media_size=2 * MIB)

        self.save_user_activity.side_effect = api_run
        self.process_gdpr_export.return_value = RunStats(processed=3, total_size=MIB)

    def _main(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            reddit_stash.main()
        return output.getvalue()

    def test_api_and_gdpr_totals_are_summed_and_the_log_is_saved(self):
        save_file_log({'old-key': {'type': 'Submission', 'file_path': 'y'}}, self.root)

        output = self._main()

        self.assertIn('5 items processed, 1 items skipped, 1 items failed', output)
        self.assertIn('Markdown file storage: 2.00 MB', output)
        self.assertIn('Media file storage: 2.00 MB', output)
        self.assertIn('Total combined storage: 4.00 MB', output)
        self.assertEqual(self.save_user_activity.call_args.kwargs, {'unsave': True})
        ctx = self.process_gdpr_export.call_args.args[1]
        self.assertEqual(ctx.existing_keys, {'old-key', 'api-key'})  # GDPR sees what the API pass just saved
        self.assertEqual(set(load_file_log(self.root)), {'old-key', 'api-key'})
        self.retry_pending_media.assert_called_once_with()

    def test_failing_media_retry_does_not_fail_a_finished_backup(self):
        self.retry_pending_media.side_effect = RuntimeError('queue broken')

        output = self._main()

        self.assertIn('Media retry pass skipped: queue broken', output)
        self.assertIn('api-key', load_file_log(self.root))

    def test_invalid_configuration_exits_1_before_touching_reddit(self):
        self.validate_configuration.side_effect = ValueError('bad settings')

        with self.assertRaises(SystemExit) as exit_info:
            self._main()

        self.assertEqual(exit_info.exception.code, 1)
        self.create_reddit_client.assert_not_called()

    def test_gdpr_pass_entries_reach_the_saved_log(self):
        def gdpr_run(reddit, ctx):
            ctx.file_log['GDPR_POST_new'] = {'type': 'GDPR_POST', 'file_path': 'z'}
            return RunStats(processed=1)

        self.process_gdpr_export.side_effect = gdpr_run

        self._main()

        self.assertIn('GDPR_POST_new', load_file_log(self.root))

    def test_failed_gdpr_pass_keeps_the_api_pass_log(self):
        self.process_gdpr_export.side_effect = RuntimeError('export broken')

        with self.assertRaisesRegex(RuntimeError, 'export broken'):
            self._main()

        self.assertIn('api-key', load_file_log(self.root))

    def test_failed_api_run_is_not_reported_as_finished(self):
        self.save_user_activity.side_effect = RuntimeError('reddit down')

        with self.assertRaisesRegex(RuntimeError, 'reddit down'):
            self._main()

        self.retry_pending_media.assert_not_called()


if __name__ == '__main__':
    unittest.main()
