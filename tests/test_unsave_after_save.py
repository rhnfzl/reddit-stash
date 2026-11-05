"""An item is unsaved on Reddit only after its file is closed and logged."""

import os
import tempfile
import unittest
from unittest.mock import Mock, patch

from utils.file_operations import SaveContext, SaveStatus, save_to_file


def _write_body(item, f):
    f.write('kept body')
    return 0


class TestUnsaveAfterSave(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.save_dir = os.path.realpath(tmp.name)
        self.path = os.path.join(self.save_dir, 'python', 'POST_abc.md')
        self.ctx = SaveContext(self.save_dir, set(), {}, set())
        self.item = Mock(id='abc')
        self.item.subreddit.display_name = 'python'

    def _save(self, save_function=_write_body):
        return save_to_file(self.item, self.path, save_function, self.ctx, unsave=True)

    def test_unsaves_only_after_the_file_is_closed_and_logged(self):
        seen = {}

        def record_state():
            with open(self.path, encoding='utf-8') as f:
                seen['body'] = f.read()
            seen['logged'] = len(self.ctx.file_log)

        self.item.unsave.side_effect = record_state

        self.assertEqual(self._save()[0], SaveStatus.SAVED)
        self.assertEqual(seen, {'body': 'kept body', 'logged': 1})

    def test_failed_write_keeps_the_item_saved(self):
        def fail_mid_write(item, f):
            f.write('partial')
            raise RuntimeError('save failed')

        self.assertEqual(self._save(fail_mid_write)[0], SaveStatus.FAILED)
        self.item.unsave.assert_not_called()

    def test_failed_log_step_keeps_the_item_saved(self):
        with patch('utils.file_operations.record_saved_file', side_effect=OSError('disk full')):
            self.assertEqual(self._save()[0], SaveStatus.FAILED)
        self.item.unsave.assert_not_called()

    def test_failed_unsave_only_warns_and_keeps_the_file(self):
        self.item.unsave.side_effect = RuntimeError('no network')

        with self.assertLogs('utils.file_operations', level='WARNING') as logs:
            self.assertEqual(self._save()[0], SaveStatus.SAVED)

        self.assertTrue(os.path.exists(self.path))
        self.assertIn('no network', logs.output[0])

    def test_unsave_is_off_by_default(self):
        save_to_file(self.item, self.path, _write_body, self.ctx)
        self.item.unsave.assert_not_called()


if __name__ == '__main__':
    unittest.main()
