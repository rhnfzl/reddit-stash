"""Tests for temporary-file lifecycle helpers."""

import tempfile
import unittest
from pathlib import Path

from utils.media_services.temp_file_utils import temp_files_cleanup


class TestTemporaryFileUtilities(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.temp_dir.cleanup()

    def _create_file(self, name):
        path = Path(self.temp_dir.name) / name
        path.write_text('content', encoding='utf-8')
        return path

    def test_files_are_removed_after_context(self):
        first = self._create_file('first.tmp')
        second = self._create_file('second.tmp')

        with temp_files_cleanup(first, second):
            self.assertTrue(first.exists())
            self.assertTrue(second.exists())

        self.assertFalse(first.exists())
        self.assertFalse(second.exists())

    def test_files_are_removed_after_exception(self):
        path = self._create_file('failed.tmp')

        with self.assertRaisesRegex(RuntimeError, 'download failed'):
            with temp_files_cleanup(path):
                raise RuntimeError('download failed')

        self.assertFalse(path.exists())

    def test_nonexistent_files_are_ignored(self):
        missing = Path(self.temp_dir.name) / 'missing.tmp'

        with temp_files_cleanup(missing, None):
            pass

        self.assertFalse(missing.exists())


if __name__ == '__main__':
    unittest.main()
