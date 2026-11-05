"""file_log.json checkpoints and durability."""

import json
import os
import stat
import tempfile
import unittest
from unittest.mock import patch

from utils.log_utils import (
    load_file_log, record_saved_file, save_file_log,
)


class TestFileLogCheckpoint(unittest.TestCase):
    def test_checkpoint_counts_saves_not_log_size(self):
        with tempfile.TemporaryDirectory() as save_dir, \
                patch("utils.log_utils.FILE_LOG_CHECKPOINT_INTERVAL", 3), \
                patch("utils.log_utils._saves_since_checkpoint", 0), \
                patch("utils.log_utils.save_file_log") as save:
            # Overwrites keep the size at 1: never a multiple of 3, but still saves to protect.
            log = {}
            for _ in range(3):
                record_saved_file(log, "same", {"file_path": os.path.join(save_dir, "a.md")}, save_dir)
            self.assertEqual(save.call_count, 1)

            # At a size that is a multiple of 3, overwrites must not rewrite the log each time.
            save.reset_mock()
            log = {"a": {}, "b": {}, "c": {}}
            for _ in range(3):
                record_saved_file(log, "a", {"file_path": os.path.join(save_dir, "a.md")}, save_dir)
            self.assertEqual(save.call_count, 1)

    def test_failed_checkpoint_waits_a_full_interval_before_trying_again(self):
        with tempfile.TemporaryDirectory() as save_dir, \
                patch("utils.log_utils.FILE_LOG_CHECKPOINT_INTERVAL", 3), \
                patch("utils.log_utils._saves_since_checkpoint", 2), \
                patch("utils.log_utils.save_file_log", side_effect=OSError("disk full")) as save:
            log = {}
            with self.assertRaises(OSError):
                record_saved_file(log, "a", {"file_path": os.path.join(save_dir, "a.md")}, save_dir)
            record_saved_file(log, "b", {"file_path": os.path.join(save_dir, "b.md")}, save_dir)
            self.assertEqual(save.call_count, 1)


class TestFileLogDurability(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.save_dir = self.tmp.name
        self.log_path = os.path.join(self.save_dir, "file_log.json")
        with open(self.log_path, "w") as f:
            json.dump({"old": {"file_path": "x"}}, f)

    def _partial_dump(self, data, f, **kwargs):
        f.write('{"partial"')
        raise OSError("disk full")

    def test_crash_during_save_keeps_the_previous_log(self):
        with patch("utils.log_utils.json.dump", side_effect=self._partial_dump), \
                self.assertRaises(OSError):
            save_file_log({"new": {}}, self.save_dir)

        self.assertEqual(load_file_log(self.save_dir), {"old": {"file_path": "x"}})
        self.assertEqual(os.listdir(self.save_dir), ["file_log.json"])

    def test_temp_file_is_a_dotfile_so_uploads_skip_it(self):
        seen = []

        def record_dir(data, f, **kwargs):
            seen.extend(set(os.listdir(self.save_dir)) - {"file_log.json"})

        with patch("utils.log_utils.json.dump", side_effect=record_dir):
            save_file_log({}, self.save_dir)

        self.assertEqual(seen, [".file_log.json.tmp"])

    def test_save_replaces_the_log_and_keeps_its_permissions(self):
        os.chmod(self.log_path, 0o640)
        save_file_log({"new": {}}, self.save_dir)

        self.assertEqual(load_file_log(self.save_dir), {"new": {}})
        self.assertEqual(stat.S_IMODE(os.stat(self.log_path).st_mode), 0o640)

    def test_new_log_gets_default_permissions_rather_than_private_ones(self):
        os.remove(self.log_path)
        old_umask = os.umask(0o022)
        self.addCleanup(os.umask, old_umask)
        save_file_log({}, self.save_dir)

        self.assertEqual(stat.S_IMODE(os.stat(self.log_path).st_mode), 0o644)

    def test_damaged_log_raises_an_error_naming_the_file(self):
        with open(self.log_path, "w") as f:
            f.write('{"partial"')

        with self.assertRaisesRegex(ValueError, "file_log.json is damaged.*storage_utils.py --download"):
            load_file_log(self.save_dir)

    def test_clean_items_crash_during_save_keeps_the_previous_log(self):
        from clean_items import clean_items

        log = {"a-sub-Submission-SAVED_POST": {"file_path": "sub/SAVED_POST_a.md"}}
        with open(self.log_path, "w") as f:
            json.dump(log, f)

        with patch("utils.log_utils.json.dump", side_effect=self._partial_dump), \
                patch("builtins.print"):
            clean_items(["saved"], reddit_dir=self.save_dir)

        with open(self.log_path) as f:
            self.assertEqual(json.load(f), log)


if __name__ == '__main__':
    unittest.main()
