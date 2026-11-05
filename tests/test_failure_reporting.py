"""Failures must be reported as failures, never recorded as backed-up items."""

import os
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

import prawcore

from tests.helpers import needs_dropbox, praw_comment, praw_submission
from utils.constants import MAX_ITEM_FAILURES
from utils.file_operations import (
    SaveContext, SaveStatus, _SUBMISSIONS, _fetch_items, _save_listing_items, get_existing_keys_from_dir,
    activity_save_context, save_to_file, save_user_activity,
)
from utils.log_utils import (
    get_existing_keys, get_given_up_keys, is_file_logged,
)
from utils.storage.base import SyncResult


KEY = "abc-python-Mock-POST"


def _item(item_id="abc"):
    item = Mock()
    item.id = item_id
    item.subreddit.display_name = "python"
    return item


class TestSaveToFileFailures(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.save_dir = self.tmp.name
        self.file_path = os.path.join(self.save_dir, "python", "POST_abc.md")

    def tearDown(self):
        self.tmp.cleanup()

    def _save(self, save_function, file_log):
        return save_to_file(_item(), self.file_path, save_function,
                            SaveContext(self.save_dir, set(), file_log, set()))

    def test_failed_save_reports_failed_and_removes_partial_file(self):
        def boom(content, f, **kwargs):
            f.write("partial")
            raise RuntimeError("network")

        file_log = {}
        status, media_size = self._save(boom, file_log)

        self.assertIs(status, SaveStatus.FAILED)
        self.assertEqual(media_size, 0)
        self.assertFalse(os.path.exists(self.file_path))
        self.assertEqual(file_log, {KEY: {"status": "failed", "attempts": 1, "last_error": "network"}})
        self.assertNotIn(KEY, get_existing_keys(file_log))

    def test_key_inserted_before_failed_checkpoint_is_dropped(self):
        def log_then_fail(file_log, key, info, save_directory):
            file_log[key] = info
            raise OSError("disk full")

        file_log = {}
        with patch("utils.file_operations.record_saved_file", side_effect=log_then_fail):
            status, _ = self._save(lambda content, f, **kw: f.write("ok"), file_log)

        self.assertIs(status, SaveStatus.FAILED)
        self.assertEqual(file_log[KEY]["status"], "failed")
        self.assertNotIn("file_path", file_log[KEY])

    def test_attempts_accumulate_until_the_item_is_given_up(self):
        def boom(content, f, **kwargs):
            raise RuntimeError("network")

        file_log = {}
        for _ in range(MAX_ITEM_FAILURES):
            self.assertIn(KEY, {KEY} - get_existing_keys(file_log))  # still retried
            self._save(boom, file_log)

        self.assertEqual(file_log[KEY]["attempts"], MAX_ITEM_FAILURES)
        self.assertIn(KEY, get_existing_keys(file_log))
        self.assertTrue(is_file_logged(file_log, KEY))
        self.assertEqual(get_given_up_keys(file_log), [KEY])

    def test_checkpoint_failure_after_log_write_keeps_attempt_count(self):
        file_log = {KEY: {"status": "failed", "attempts": 3, "last_error": "x"}}

        def log_then_fail(log, key, info, save_directory):
            log[key] = info  # record_saved_file replaced the failure entry, then the checkpoint failed
            raise OSError("disk full")

        with patch("utils.file_operations.record_saved_file", side_effect=log_then_fail):
            self._save(lambda content, f, **kw: f.write("ok"), file_log)

        self.assertEqual(file_log[KEY]["attempts"], 4)

    def test_dir_scan_keys_match_save_keys_so_saved_files_are_skipped(self):
        os.makedirs(os.path.dirname(self.file_path))
        with open(self.file_path, "w") as f:
            f.write("existing backup")
        existing = get_existing_keys_from_dir(self.save_dir)

        def boom(content, f, **kwargs):
            raise RuntimeError("must not be called")

        class Submission:  # key uses the class name, like praw's Submission
            id = "abc"
            subreddit = Mock(display_name="python")

        status, _ = save_to_file(Submission(), self.file_path, boom,
                                 SaveContext(self.save_dir, existing, {}, set()))

        self.assertIs(status, SaveStatus.SKIPPED)
        with open(self.file_path) as f:
            self.assertEqual(f.read(), "existing backup")

    def test_success_after_failure_replaces_the_failure_entry(self):
        file_log = {KEY: {"status": "failed", "attempts": 2, "last_error": "x"}}

        status, _ = self._save(lambda content, f, **kw: f.write("ok"), file_log)

        self.assertIs(status, SaveStatus.SAVED)
        self.assertNotIn("status", file_log[KEY])
        self.assertEqual(file_log[KEY]["file_path"], os.path.join("python", "POST_abc.md"))

    def test_batch_counts_failed_items_separately(self):
        outcomes = iter([(SaveStatus.SAVED, 5), (SaveStatus.SKIPPED, 0), (SaveStatus.FAILED, 0)])
        items = [praw_submission(item_id) for item_id in "abc"]
        with patch("utils.file_operations.save_to_file", side_effect=lambda *a, **k: next(outcomes)), \
                patch("utils.file_operations.sleep_for_item"):
            stats = _save_listing_items(items, SaveContext(self.save_dir, set(), {}, set()), _SUBMISSIONS, False, 0)

        self.assertEqual((stats.processed, stats.skipped, stats.failed, stats.media_size), (1, 1, 1, 5))


class TestTruncatedListings(unittest.TestCase):
    def _reddit(self, listing_calls):
        reddit = Mock()
        reddit.user.me.return_value.saved.side_effect = listing_calls
        return reddit

    def test_listing_that_404s_twice_is_reported_with_its_partial_items(self):
        def fails_after_one():
            yield "first"
            raise prawcore.exceptions.NotFound(Mock())

        reddit = self._reddit(lambda limit: fails_after_one())
        self.assertEqual(_fetch_items(reddit, "saved", 10, "saved"), (["first"], True))

    def test_listing_refused_with_403_on_first_read_fails_the_run(self):
        def refused():
            raise prawcore.exceptions.Forbidden(Mock())
            yield

        reddit = self._reddit([refused(), refused()])
        with tempfile.TemporaryDirectory() as save_dir, \
                patch("utils.file_operations.load_settings") as settings, \
                self.assertRaises(prawcore.exceptions.Forbidden):
            settings.return_value.get.side_effect = lambda section, key, fallback: {
                "save_type": "SAVED", "check_type": "LOG"}[key]
            save_user_activity(reddit, activity_save_context(save_dir, {}))

    def test_403_on_the_retry_after_a_404_keeps_the_partial_listing(self):
        def not_found():
            raise prawcore.exceptions.NotFound(Mock())
            yield

        def refused_after_one():
            yield "first"
            raise prawcore.exceptions.Forbidden(Mock())

        reddit = self._reddit([not_found(), refused_after_one()])
        self.assertEqual(_fetch_items(reddit, "saved", 10, "saved"), (["first"], True))

    def test_listing_that_recovers_on_retry_is_not_reported(self):
        def fails():
            raise prawcore.exceptions.NotFound(Mock())
            yield

        reddit = self._reddit([fails(), iter(["a", "b"])])
        self.assertEqual(_fetch_items(reddit, "saved", 10, "saved"), (["a", "b"], False))

    def test_save_user_activity_returns_truncated_listing_labels(self):
        with tempfile.TemporaryDirectory() as save_dir, \
                patch("utils.file_operations.load_settings") as settings, \
                patch("utils.file_operations._fetch_items", return_value=([], True)):
            settings.return_value.get.side_effect = lambda section, key, fallback: {
                "save_type": "SAVED", "check_type": "LOG"}[key]
            stats = save_user_activity(Mock(), activity_save_context(save_dir, {}))

        self.assertEqual(stats.truncated_listings, ("saved",))


class TestSaveRunWiring(unittest.TestCase):
    def test_all_mode_saves_each_listing_under_its_own_categories(self):
        submissions = [praw_submission("sub")]
        comments = [praw_comment("com")]
        saved = [praw_submission("sav"), praw_comment("savc")]
        upvoted = [praw_submission("upv"), praw_comment("upvc")]
        listing = {"submissions": submissions, "comments": comments, "saved": saved, "upvoted": upvoted}
        unsaved = []

        def save_fn_stub(item, f):
            return 0

        with tempfile.TemporaryDirectory() as save_dir, \
                patch("utils.file_operations.load_settings") as settings, \
                patch("utils.file_operations._clone_reddit"), \
                patch("utils.file_operations._fetch_items",
                      side_effect=lambda r, method, limit, label: (listing[method], False)), \
                patch("utils.file_operations.save_submission", save_fn_stub), \
                patch("utils.file_operations.save_comment_and_context", save_fn_stub), \
                patch("utils.file_operations._unsave", side_effect=lambda item: unsaved.append(item.id)), \
                patch("utils.file_operations.sleep_for_item"):
            settings.return_value.get.side_effect = lambda section, key, fallback: {
                "save_type": "ALL", "check_type": "LOG"}[key]
            file_log = {}
            # realpath: temp dirs sit behind a symlink on macOS and paths are resolved
            stats = save_user_activity(Mock(), activity_save_context(os.path.realpath(save_dir), file_log), unsave=True)

        self.assertEqual(stats.processed, 6)
        self.assertEqual(stats.truncated_listings, ())
        self.assertEqual(sorted(unsaved), ["sav", "savc"])
        self.assertEqual(set(file_log), {
            "sub-python-Submission-POST",
            "com-python-Comment-COMMENT",
            "sav-python-Submission-SAVED_POST",
            "savc-python-Comment-SAVED_COMMENT",
            "upv-python-Submission-UPVOTE_POST",
            "upvc-python-Comment-UPVOTE_COMMENT",
        })
        self.assertEqual(file_log["com-python-Comment-COMMENT"]["file_path"],
                         os.path.join("python", "COMMENT_com.md"))


    def _run_mode(self, save_type, unsave=False):
        """Run one save_type over distinct listings; return (file_log keys, unsaved ids, fetch threads, clone mock)."""
        listing = {
            "submissions": [praw_submission("sub")],
            "comments": [praw_comment("com")],
            "saved": [praw_submission("sav")],
            "upvoted": [praw_comment("upv")],
        }
        unsaved, threads = [], []

        def fetch(_reddit, method, _limit, _label):
            threads.append(threading.current_thread())
            return listing[method], False

        def save_fn_stub(item, f):
            threads.append(threading.current_thread())
            return 0

        file_log = {}
        with tempfile.TemporaryDirectory() as save_dir, \
                patch("utils.file_operations.load_settings") as settings, \
                patch("utils.file_operations._clone_reddit") as clone, \
                patch("utils.file_operations._fetch_items", side_effect=fetch) as fetch_mock, \
                patch("utils.file_operations.save_submission", save_fn_stub), \
                patch("utils.file_operations.save_comment_and_context", save_fn_stub), \
                patch("utils.file_operations._unsave", side_effect=lambda item: unsaved.append(item.id)), \
                patch("utils.file_operations.sleep_for_item"):
            settings.return_value.get.side_effect = lambda section, key, fallback: {
                "save_type": save_type, "check_type": "LOG"}[key]
            save_user_activity(self.reddit, activity_save_context(os.path.realpath(save_dir), file_log), unsave=unsave)
        return set(file_log), unsaved, threads, clone, fetch_mock

    def setUp(self):
        self.reddit = Mock()

    def test_saved_mode_saves_only_saved_items_and_honours_unsave(self):
        keys, unsaved, _, _, _ = self._run_mode("SAVED", unsave=True)
        self.assertEqual(keys, {"sav-python-Submission-SAVED_POST"})
        self.assertEqual(unsaved, ["sav"])

    def test_upvoted_mode_saves_only_upvoted_items_and_never_unsaves(self):
        keys, unsaved, _, _, _ = self._run_mode("UPVOTED", unsave=True)
        self.assertEqual(keys, {"upv-python-Comment-UPVOTE_COMMENT"})
        self.assertEqual(unsaved, [])

    def test_activity_mode_saves_submissions_and_comments(self):
        keys, unsaved, _, _, _ = self._run_mode("ACTIVITY", unsave=True)
        self.assertEqual(keys, {"sub-python-Submission-POST", "com-python-Comment-COMMENT"})
        self.assertEqual(unsaved, [])

    def test_single_listing_uses_the_given_reddit_in_the_main_thread(self):
        # In a worker, a SIGTERM exception in --cycle would wait for the whole listing.
        for save_type in ("SAVED", "UPVOTED"):
            with self.subTest(save_type=save_type):
                _, _, threads, clone, fetch_mock = self._run_mode(save_type)
                clone.assert_not_called()
                self.assertIs(fetch_mock.call_args.args[0], self.reddit)
                self.assertEqual(set(threads), {threading.main_thread()})

    def test_several_listings_each_get_their_own_reddit_instance(self):
        _, _, _, clone, fetch_mock = self._run_mode("ACTIVITY")
        self.assertEqual(clone.call_count, 2)
        self.assertNotIn(self.reddit, [call.args[0] for call in fetch_mock.call_args_list])


class TestStorageCliExitCodes(unittest.TestCase):
    def test_failed_or_errored_sync_exits_non_zero(self):
        from storage_utils import _exit_on_failure

        for result in (SyncResult(failed=1), SyncResult(errors=["boom"])):
            with self.subTest(result=result), self.assertRaises(SystemExit) as ctx:
                _exit_on_failure(result)
            self.assertEqual(ctx.exception.code, 1)

    def test_clean_sync_does_not_exit(self):
        from storage_utils import _exit_on_failure

        _exit_on_failure(SyncResult(downloaded=3))


@needs_dropbox
class TestDropboxListing(unittest.TestCase):
    def _provider(self, error):
        import utils.storage.dropbox_provider as dp

        class FakeApiError(Exception):
            def __init__(self, err):
                super().__init__("api error")
                self.error = err

        provider = dp.DropboxStorageProvider()
        provider._dbx = Mock()
        provider._dbx.files_list_folder.side_effect = FakeApiError(error)
        patcher = patch.object(dp, "ApiError", FakeApiError)
        patcher.start()
        self.addCleanup(patcher.stop)
        return provider

    def test_missing_folder_is_empty(self):
        err = Mock()
        err.is_path.return_value = True
        err.get_path.return_value.is_not_found.return_value = True

        self.assertEqual(self._provider(err).list_files("/reddit"), [])

    def test_other_errors_raise_instead_of_returning_partial_list(self):
        err = Mock()
        err.is_path.return_value = False

        with self.assertRaises(RuntimeError):
            self._provider(err).list_files("/reddit")


class TestConfigFailureExit(unittest.TestCase):
    @patch("reddit_stash.validate_configuration", side_effect=ValueError("bad"))
    def test_invalid_configuration_exits_non_zero(self, _validate):
        import reddit_stash

        with self.assertRaises(SystemExit) as ctx:
            reddit_stash.main()
        self.assertEqual(ctx.exception.code, 1)


if __name__ == "__main__":
    unittest.main()
