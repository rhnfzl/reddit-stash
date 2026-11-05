"""clean_items CLI: selection, --reddit-dir, dry run and failure reporting."""

import contextlib
import io
import json
import os
import stat
import tempfile
import unittest
from unittest.mock import patch

from clean_items import main


class CleanItemsCliTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.cwd = tmp.name
        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(self.cwd)
        self.reddit_dir = os.path.join(self.cwd, "other")
        os.makedirs(os.path.join(self.reddit_dir, "sub"))
        self.log_path = os.path.join(self.reddit_dir, "file_log.json")

    def _write_log(self, log, path=None):
        with open(path or self.log_path, "w") as f:
            json.dump(log, f)

    def _read_log(self, path=None):
        with open(path or self.log_path) as f:
            return json.load(f)

    def _add_file(self, name):
        path = os.path.join(self.reddit_dir, "sub", name)
        open(path, "w").close()
        return path

    def _run(self, *flags):
        """Run the CLI and return (exit code, printed output)."""
        out = io.StringIO()
        code = 0
        with patch("sys.argv", ["clean_items.py", "--reddit-dir", self.reddit_dir, *flags]), \
                contextlib.redirect_stdout(out):
            try:
                main()
            except SystemExit as e:
                code = e.code
        return code, out.getvalue()

    def test_custom_reddit_dir_cleans_its_own_log_not_the_default(self):
        default_log = {"a-sub-Submission-SAVED_POST": {"file_path": "sub/SAVED_POST_a.md"}}
        os.makedirs("reddit")
        self._write_log(default_log, "reddit/file_log.json")
        self._write_log({"b-sub-Submission-SAVED_POST": {"file_path": "sub/SAVED_POST_b.md"}})

        code, _ = self._run("--saved")

        self.assertEqual(code, 0)
        self.assertEqual(self._read_log(), {})
        self.assertEqual(self._read_log("reddit/file_log.json"), default_log)

    def test_keys_match_on_the_category_not_on_the_subreddit_name(self):
        kept = {
            "a-UPVOTEmemes-Comment-COMMENT": {},
            "b-SAVED_POST-Comment-COMMENT": {},
            # Enriched GDPR comments are logged under POST but are not submissions.
            "c-sub-Comment-POST": {"file_path": "sub/GDPR_COMMENT_c.md"},
        }
        removed = {
            "d-sub-Submission-UPVOTE_POST": {},
            "e-sub-Comment-UPVOTE_COMMENT": {},
            "f-sub-Submission-SAVED_POST": {},
            "g-sub-Comment-SAVED_COMMENT": {},
            "h-sub-Submission-POST": {},
            # Keys from before categories existed only match through their file name.
            "i-sub-Submission": {"file_path": "sub/POST_i.md"},
        }
        self._write_log({**kept, **removed})

        code, _ = self._run("--upvoted", "--saved", "--submissions")

        self.assertEqual(code, 0)
        self.assertEqual(self._read_log(), kept)

    def test_dry_run_reports_what_the_real_run_removes_and_changes_nothing(self):
        log = {
            "a-sub-Submission-SAVED_POST": {},
            "b-sub-Submission-POST": {},
        }
        self._write_log(log)
        saved = self._add_file("SAVED_POST_a.md")
        other = self._add_file("POST_b.md")

        code, out = self._run("--saved", "--dry-run")

        self.assertEqual(code, 0)
        self.assertIn("a-sub-Submission-SAVED_POST", out)
        self.assertNotIn("b-sub-Submission-POST", out)
        self.assertIn("SAVED_POST_a.md", out)
        self.assertEqual(self._read_log(), log)
        self.assertTrue(os.path.exists(saved))
        self.assertTrue(os.path.exists(other))

    def test_damaged_log_exits_non_zero_and_keeps_the_files(self):
        with open(self.log_path, "w") as f:
            f.write('{"partial"')
        saved = self._add_file("SAVED_POST_a.md")

        code, out = self._run("--saved")

        self.assertEqual(code, 1)
        self.assertNotIn("Cleanup Complete", out)
        self.assertTrue(os.path.exists(saved))

    def test_failed_file_removal_exits_non_zero(self):
        self._write_log({})
        self._add_file("SAVED_POST_a.md")

        with patch("os.remove", side_effect=PermissionError("denied")):
            code, out = self._run("--saved")

        self.assertEqual(code, 1)
        self.assertNotIn("Cleanup Complete", out)

    def test_comments_flag_removes_only_user_comments(self):
        kept = {"a-sub-Submission-POST": {}, "b-sub-Comment-SAVED_COMMENT": {}}
        self._write_log({**kept, "c-sub-Comment-COMMENT": {}, "d-sub-Submission-COMMENT": {}})
        comment = self._add_file("COMMENT_c.md")
        others = [self._add_file("POST_a.md"), self._add_file("SAVED_COMMENT_b.md")]

        code, _ = self._run("--comments")

        self.assertEqual(code, 0)
        self.assertEqual(self._read_log(), kept)
        self.assertFalse(os.path.exists(comment))
        self.assertTrue(all(os.path.exists(path) for path in others))

    def test_all_flag_removes_every_type_and_keeps_gdpr_items(self):
        kept = {"GDPR_POST_a": {}, "GDPR_COMMENT_b": {}, "c-sub-Comment-POST": {}}
        self._write_log({
            **kept,
            "d-sub-Submission-UPVOTE_POST": {},
            "e-sub-Comment-SAVED_COMMENT": {},
            "f-sub-Comment-COMMENT": {},
            "g-sub-Submission-POST": {},
        })
        removed = [self._add_file(f"{prefix}_x.md") for prefix in ("UPVOTE_POST", "SAVED_COMMENT", "COMMENT", "POST")]
        gdpr = [self._add_file("GDPR_POST_a.md"), self._add_file("GDPR_COMMENT_b.md")]

        code, _ = self._run("--all")

        self.assertEqual(code, 0)
        self.assertEqual(self._read_log(), kept)
        self.assertFalse(any(os.path.exists(path) for path in removed))
        self.assertTrue(all(os.path.exists(path) for path in gdpr))

    def test_each_flag_removes_only_its_own_files_and_log_keys(self):
        everything = {
            "UPVOTE_POST": "a-sub-Submission-UPVOTE_POST", "UPVOTE_COMMENT": "b-sub-Comment-UPVOTE_COMMENT",
            "SAVED_POST": "c-sub-Submission-SAVED_POST", "SAVED_COMMENT": "d-sub-Comment-SAVED_COMMENT",
            "COMMENT": "e-sub-Comment-COMMENT", "POST": "f-sub-Submission-POST",
            "GDPR_POST": "GDPR_POST_g", "GDPR_COMMENT": "GDPR_COMMENT_h",
        }
        flags = {
            "--upvoted": {"UPVOTE_POST", "UPVOTE_COMMENT"},
            "--saved": {"SAVED_POST", "SAVED_COMMENT"},
            "--comments": {"COMMENT"},
            "--submissions": {"POST"},
        }
        for flag, removed in flags.items():
            with self.subTest(flag=flag):
                self._write_log({key: {} for key in everything.values()})
                paths = {category: self._add_file(f"{category}_x.md") for category in everything}

                code, _ = self._run(flag)

                self.assertEqual(code, 0)
                self.assertEqual(set(self._read_log()), {k for c, k in everything.items() if c not in removed})
                self.assertEqual({c for c, path in paths.items() if not os.path.exists(path)}, removed)

    def test_no_item_type_is_a_usage_error_and_removes_nothing(self):
        saved = self._add_file("SAVED_POST_a.md")

        with contextlib.redirect_stderr(io.StringIO()):
            code, _ = self._run()

        self.assertEqual(code, 2)
        self.assertTrue(os.path.exists(saved))

    def test_log_that_is_not_an_object_exits_non_zero_and_keeps_the_files(self):
        self._write_log(["not", "a", "log"])
        saved = self._add_file("SAVED_POST_a.md")

        code, out = self._run("--saved")

        self.assertEqual(code, 1)
        self.assertNotIn("Cleanup Complete", out)
        self.assertTrue(os.path.exists(saved))

    def test_entry_with_non_string_file_path_exits_non_zero_and_keeps_the_files(self):
        log = {"a-sub-Submission-POST": {"file_path": 7}}
        self._write_log(log)
        saved = self._add_file("SAVED_POST_a.md")

        code, out = self._run("--saved")

        self.assertEqual(code, 1)
        self.assertNotIn("Cleanup Complete", out)
        self.assertEqual(self._read_log(), log)
        self.assertTrue(os.path.exists(saved))

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root reads every directory")
    def test_unreadable_subdirectory_exits_non_zero(self):
        self._write_log({})
        sub = os.path.join(self.reddit_dir, "sub")
        self._add_file("SAVED_POST_a.md")
        os.chmod(sub, 0)
        self.addCleanup(os.chmod, sub, stat.S_IRWXU)

        code, out = self._run("--saved")

        self.assertEqual(code, 1)
        self.assertNotIn("Cleanup Complete", out)

    def test_missing_reddit_dir_is_not_an_error(self):
        self.reddit_dir = os.path.join(self.cwd, "absent")

        code, out = self._run("--saved")

        self.assertEqual(code, 0)
        self.assertIn("No saved items found", out)


if __name__ == "__main__":
    unittest.main()
