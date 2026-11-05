"""Recovered media relinks its markdown, and retried rows keep their attempt count."""

import os
import tempfile
import unittest
from unittest.mock import patch

from utils.save_utils import relink_media_line


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

            relink_media_line(self.URL, local)

            self.assertEqual(self._read(own), f"![Image]({local})\nSee [this]({self.URL}) for context\n")
            self.assertEqual(self._read(neighbour), f"![Image]({self.URL})\n")

    def test_relink_reports_whether_a_line_now_points_at_the_file(self):
        with tempfile.TemporaryDirectory() as d:
            self._write(d, "POST_abc.md", f"![Image]({self.URL})\n")

            self.assertTrue(relink_media_line(self.URL, os.path.join(d, "abc.jpg")))
            self.assertFalse(relink_media_line(self.URL, os.path.join(d, "abc.jpg")))
            self.assertFalse(relink_media_line(self.URL, os.path.join(d, "zzz.jpg")))
            self.assertFalse(relink_media_line(self.URL, os.path.join(d, "missing", "abc.jpg")))

    def test_gallery_media_maps_to_its_post(self):
        with tempfile.TemporaryDirectory() as d:
            own = self._write(d, "POST_abc.md", f"![Gallery Image 2]({self.URL})\n")
            local = os.path.join(d, "abc_g2.jpg")

            relink_media_line(self.URL, local)

            self.assertEqual(self._read(own), f"![Gallery Image 2]({local})\n")

    def test_blockquoted_comment_image_is_relinked(self):
        with tempfile.TemporaryDirectory() as d:
            own = self._write(d, "SAVED_COMMENT_abc.md", f"> ![Image]({self.URL})\n")
            local = os.path.join(d, "abc.jpg")

            relink_media_line(self.URL, local)

            self.assertEqual(self._read(own), f"> ![Image]({local})\n")

    def test_relink_keeps_the_post_permissions(self):
        with tempfile.TemporaryDirectory() as d:
            own = self._write(d, "POST_abc.md", f"![Image]({self.URL})\n")
            os.chmod(own, 0o644)

            relink_media_line(self.URL, os.path.join(d, "abc.jpg"))

            self.assertEqual(self._read(own), f"![Image]({os.path.join(d, 'abc.jpg')})\n")
            self.assertEqual(os.stat(own).st_mode & 0o777, 0o644)

    def test_failed_write_keeps_the_original_post(self):
        with tempfile.TemporaryDirectory() as d:
            own = self._write(d, "POST_abc.md", f"![Image]({self.URL})\n")

            with patch("utils.save_utils.os.replace", side_effect=OSError("disk full")):
                relink_media_line(self.URL, os.path.join(d, "abc.jpg"))

            self.assertEqual(self._read(own), f"![Image]({self.URL})\n")
            self.assertEqual(sorted(os.listdir(d)), ["POST_abc.md"])


class TestRetryCountDoesNotReset(unittest.TestCase):
    def test_failed_retry_increments_instead_of_requeueing(self):
        from utils.media_services.retry_queue import SQLiteRetryQueue

        with tempfile.TemporaryDirectory() as d:
            queue = SQLiteRetryQueue(os.path.join(d, "q.db"))
            url = "https://i.imgur.com/x.jpg"
            queue.add_failed_download(url, "timeout", "imgur")
            with queue.sqlite_manager.get_connection() as conn:
                conn.execute("UPDATE retry_queue SET next_retry_at = 0")
                conn.commit()

            self.assertTrue(queue.mark_retry_started(url, "imgur"))
            queue.add_failed_download(url, "timeout again", "imgur")
            queue.record_retry_outcome(url, "imgur", success=False)

            with queue.sqlite_manager.get_connection() as conn:
                count = conn.execute("SELECT retry_count FROM retry_queue WHERE url = ?", (url,)).fetchone()
            self.assertEqual(count[0], 1)

    def test_rows_stuck_in_progress_become_pending_again(self):
        from utils.media_services.retry_queue import SQLiteRetryQueue

        with tempfile.TemporaryDirectory() as d:
            queue = SQLiteRetryQueue(os.path.join(d, "q.db"))
            url = "https://i.imgur.com/y.jpg"
            queue.add_failed_download(url, "timeout", "imgur")
            with queue.sqlite_manager.get_connection() as conn:
                conn.execute("UPDATE retry_queue SET status = 'in_progress', next_retry_at = 0")
                conn.commit()

            self.assertEqual(queue.reset_stale_in_progress(), 1)
            self.assertEqual([r.url for r in queue.get_pending_retries()], [url])


if __name__ == '__main__':
    unittest.main()
