"""Failures must be reported as failures, never recorded as backed-up items."""

import os
import tempfile
import unittest
from unittest.mock import Mock, patch

from utils.file_operations import save_to_file, _process_submissions_batch
from utils.media_download_manager import _relink_markdown
from utils.storage.base import SyncResult


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
        self.assertEqual(file_log, {})

    def test_key_inserted_before_failed_checkpoint_is_dropped(self):
        def log_then_fail(file_log, key, info, save_directory):
            file_log[key] = info
            raise OSError("disk full")

        file_log = {}
        with patch("utils.file_operations.log_file", side_effect=log_then_fail):
            status, _ = self._save(lambda content, f, **kw: f.write("ok"), file_log)

        self.assertIsNone(status)
        self.assertEqual(file_log, {})

    def test_existing_log_entry_survives_a_failed_resave(self):
        key = "abc-python-Mock-POST"
        file_log = {key: {"file_path": "python/POST_abc.md"}}

        def boom(content, f, **kwargs):
            raise RuntimeError("network")

        status, _ = self._save(boom, file_log)

        self.assertIsNone(status)
        self.assertIn(key, file_log)

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
    def test_remote_link_is_replaced_with_local_path(self):
        with tempfile.TemporaryDirectory() as d:
            md = os.path.join(d, "POST_abc.md")
            other = os.path.join(d, "POST_xyz.md")
            with open(md, "w", encoding="utf-8") as f:
                f.write("![Image](https://i.redd.it/a.jpg)\n")
            with open(other, "w", encoding="utf-8") as f:
                f.write("![Image](https://i.redd.it/other.jpg)\n")
            local = os.path.join(d, "abc.jpg")

            _relink_markdown("https://i.redd.it/a.jpg", local)

            with open(md, encoding="utf-8") as f:
                self.assertEqual(f.read(), f"![Image]({local})\n")
            with open(other, encoding="utf-8") as f:
                self.assertIn("other.jpg)", f.read())


class TestConfigFailureExit(unittest.TestCase):
    @patch("reddit_stash.validate_configuration", side_effect=ValueError("bad"))
    def test_invalid_configuration_exits_non_zero(self, _validate):
        import reddit_stash

        with self.assertRaises(SystemExit) as ctx:
            reddit_stash.main()
        self.assertEqual(ctx.exception.code, 1)


if __name__ == "__main__":
    unittest.main()
