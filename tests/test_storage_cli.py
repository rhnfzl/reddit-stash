"""Tests for storage command entry points."""

import os
import unittest
from unittest.mock import patch

import dropbox_utils


class TestDropboxCompatibilityCommand(unittest.TestCase):
    def test_wrapper_selects_dropbox_before_running_storage_command(self):
        with patch.dict(os.environ, {"STORAGE_PROVIDER": "s3"}, clear=False):
            with patch.object(dropbox_utils, "storage_main") as storage_main:
                dropbox_utils.main()

            self.assertEqual(os.environ["STORAGE_PROVIDER"], "dropbox")
            storage_main.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
