"""Failures must be reported as failures, never recorded as backed-up items."""

import os
import tempfile
import unittest
from unittest.mock import Mock, patch

from utils.constants import MAX_ITEM_FAILURES
from utils.file_operations import save_to_file, _process_submissions_batch
from utils.log_utils import get_existing_keys, get_given_up_keys, is_file_logged
from utils.media_download_manager import _relink_markdown
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
        return save_to_file(_item(), self.file_path, save_function, set(), file_log,
                            self.save_dir, set())

    def test_failed_save_returns_none_and_removes_partial_file(self):
        def boom(content, f, **kwargs):
            f.write("partial")
            raise RuntimeError("network")

        file_log = {}
        status, media_size = self._save(boom, file_log)

        self.assertIsNone(status)
        self.assertEqual(media_size, 0)
        self.assertFalse(os.path.exists(self.file_path))
        self.assertEqual(file_log, {KEY: {"status": "failed", "attempts": 1, "last_error": "network"}})
        self.assertNotIn(KEY, get_existing_keys(file_log))

    def test_key_inserted_before_failed_checkpoint_is_dropped(self):
        def log_then_fail(file_log, key, info, save_directory):
            file_log[key] = info
            raise OSError("disk full")

        file_log = {}
        with patch("utils.file_operations.log_file", side_effect=log_then_fail):
            status, _ = self._save(lambda content, f, **kw: f.write("ok"), file_log)

        self.assertIsNone(status)
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

    def test_success_after_failure_replaces_the_failure_entry(self):
        file_log = {KEY: {"status": "failed", "attempts": 2, "last_error": "x"}}

        status, _ = self._save(lambda content, f, **kw: f.write("ok"), file_log)

        self.assertFalse(status)
        self.assertNotIn("status", file_log[KEY])
        self.assertEqual(file_log[KEY]["file_path"], os.path.join("python", "POST_abc.md"))

    def test_batch_counts_failed_items_separately(self):
        outcomes = iter([(False, 0), (True, 0), (None, 0)])
        with patch("utils.file_operations.save_to_file", side_effect=lambda *a, **k: next(outcomes)), \
                patch("utils.file_operations.handle_dynamic_sleep"):
            result = _process_submissions_batch(
                [_item("a"), _item("b"), _item("c")], self.save_dir, set(), set(), {}, None,
            )

        processed, skipped, _size, _media, failed = result
        self.assertEqual((processed, skipped, failed), (1, 1, 1))


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


class TestDropboxListing(unittest.TestCase):
    def _provider(self, error):
        import utils.storage.dropbox_provider as dp

        class FakeApiError(Exception):
            def __init__(self, err):
                super().__init__("api error")
                self.error = err

        provider = dp.DropboxStorageProvider(dropbox_directory="/reddit")
        provider._dbx = Mock()
        provider._dbx.files_list_folder.side_effect = FakeApiError(error)
        patcher = patch.object(dp, "_ApiError", FakeApiError)
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


class TestRelinkMarkdown(unittest.TestCase):
    URL = "https://i.redd.it/a.jpg"

    def _write(self, d, name, text):
        path = os.path.join(d, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return path

    def _read(self, path):
        with open(path, encoding="utf-8") as f:
            return f.read()

    def test_only_the_owning_posts_media_line_is_relinked(self):
        with tempfile.TemporaryDirectory() as d:
            body = f"![Image]({self.URL})\nSee [this]({self.URL}) for context\n"
            own = self._write(d, "SAVED_POST_abc.md", body)
            neighbour = self._write(d, "SAVED_POST_xyz.md", f"![Image]({self.URL})\n")
            local = os.path.join(d, "abc.jpg")

            _relink_markdown(self.URL, local)

            self.assertEqual(self._read(own), f"![Image]({local})\nSee [this]({self.URL}) for context\n")
            self.assertEqual(self._read(neighbour), f"![Image]({self.URL})\n")

    def test_gallery_media_maps_to_its_post(self):
        with tempfile.TemporaryDirectory() as d:
            own = self._write(d, "POST_abc.md", f"![Gallery Image 2]({self.URL})\n")
            local = os.path.join(d, "abc_g2.jpg")

            _relink_markdown(self.URL, local)

            self.assertEqual(self._read(own), f"![Gallery Image 2]({local})\n")

    def test_failed_write_keeps_the_original_post(self):
        with tempfile.TemporaryDirectory() as d:
            own = self._write(d, "POST_abc.md", f"![Image]({self.URL})\n")

            with patch("utils.media_download_manager.os.replace", side_effect=OSError("disk full")):
                _relink_markdown(self.URL, os.path.join(d, "abc.jpg"))

            self.assertEqual(self._read(own), f"![Image]({self.URL})\n")
            self.assertEqual(sorted(os.listdir(d)), ["POST_abc.md"])


class TestRetryCountDoesNotReset(unittest.TestCase):
    def test_failed_retry_increments_instead_of_requeueing(self):
        from utils.retry_queue import SQLiteRetryQueue

        with tempfile.TemporaryDirectory() as d:
            queue = SQLiteRetryQueue(os.path.join(d, "q.db"))
            url = "https://i.imgur.com/x.jpg"
            queue.add_failed_download(url, "timeout", "imgur")
            with queue.sqlite_manager.get_connection() as conn:
                conn.execute("UPDATE retry_queue SET next_retry_at = 0")
                conn.commit()

            self.assertTrue(queue.mark_retry_started(url, "imgur"))
            queue.add_failed_download(url, "timeout again", "imgur")  # download_media on the retry
            queue.mark_retry_completed(url, success=False)

            with queue.sqlite_manager.get_connection() as conn:
                count = conn.execute("SELECT retry_count FROM retry_queue WHERE url = ?", (url,)).fetchone()
            self.assertEqual(count[0], 1)


class TestConfigFailureExit(unittest.TestCase):
    @patch("reddit_stash.validate_configuration", side_effect=ValueError("bad"))
    def test_invalid_configuration_exits_non_zero(self, _validate):
        import reddit_stash

        with self.assertRaises(SystemExit) as ctx:
            reddit_stash.main()
        self.assertEqual(ctx.exception.code, 1)


if __name__ == "__main__":
    unittest.main()
