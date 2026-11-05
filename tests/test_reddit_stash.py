"""Tests for the application entry-point helpers."""

import io
import unittest
from unittest.mock import patch

import reddit_stash


class TestRedditClientCreation(unittest.TestCase):
    """Reddit client setup has one shared credential path."""

    @patch("reddit_stash.praw.Reddit")
    @patch("reddit_stash.load_reddit_credentials")
    def test_create_reddit_client_uses_loaded_credentials(self, load_credentials, reddit_class):
        load_credentials.return_value = ("client-id", "client-secret", "user", "password")

        client = reddit_stash.create_reddit_client()

        self.assertIs(client, reddit_class.return_value)
        reddit_class.assert_called_once_with(
            client_id="client-id",
            client_secret="client-secret",
            username="user",
            password="password",
            user_agent="Reddit Saved Saver by /u/user",
        )


class TestStartupValidation(unittest.TestCase):
    @patch("reddit_stash.get_storage_summary", return_value="Cloud storage: S3 (s3://b)")
    @patch("reddit_stash.get_feature_summary", return_value="Media downloads: DISABLED")
    @patch("reddit_stash.validate_configuration", return_value={"warnings": []})
    def test_valid_config_reports_media_and_storage(self, _validate, _feature, _storage):
        with patch("sys.stdout", new_callable=io.StringIO) as out:
            reddit_stash.validate_config_or_exit()

        self.assertIn("Media downloads: DISABLED", out.getvalue())
        self.assertIn("Cloud storage: S3 (s3://b)", out.getvalue())


if __name__ == "__main__":
    unittest.main()
