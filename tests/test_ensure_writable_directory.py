"""save_directory validation: the path when usable, an error when not."""

import contextlib
import io
import os
import tempfile
import unittest
from unittest.mock import patch

from utils.path_security import ensure_writable_directory


class TestEnsureWritableDirectory(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = tmp.name

    def _validate(self, path):
        with contextlib.redirect_stdout(io.StringIO()):
            return ensure_writable_directory(path)

    def test_writable_directory_is_returned_as_an_absolute_path(self):
        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(self.root)
        os.mkdir('saves')

        self.assertEqual(self._validate('saves'), os.path.abspath('saves'))

    def test_missing_directory_is_created(self):
        path = os.path.join(self.root, 'a', 'b')

        self.assertEqual(self._validate(path), path)
        self.assertTrue(os.path.isdir(path))

    def test_directory_that_cannot_be_created_raises_and_redirects_nowhere(self):
        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(self.root)
        blocker = os.path.join(self.root, 'file')
        open(blocker, 'w').close()

        with self.assertRaises(OSError):
            self._validate(os.path.join(blocker, 'saves'))
        self.assertEqual(os.listdir(self.root), ['file'])

    def test_unwritable_directory_raises_naming_the_path(self):
        with patch('utils.path_security.os.access', return_value=False), \
                self.assertRaisesRegex(OSError, 'not writable'):
            self._validate(self.root)


if __name__ == '__main__':
    unittest.main()
