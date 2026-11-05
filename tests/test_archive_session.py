"""The session the archive API providers share."""

import unittest

from utils.content_recovery.providers.archive_session import archive_session


class TestArchiveSession(unittest.TestCase):
    def test_identifies_the_tool_and_asks_for_json(self):
        headers = archive_session().headers

        self.assertIn('Reddit Stash', headers['User-Agent'])
        self.assertEqual(headers['Accept'], 'application/json')

    def test_each_call_gets_its_own_session(self):
        self.assertIsNot(archive_session(), archive_session())


if __name__ == '__main__':
    unittest.main()
