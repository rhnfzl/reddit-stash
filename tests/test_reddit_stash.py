"""Tests for the application entry-point helpers."""

import unittest
from unittest.mock import patch

import reddit_stash


class TestRedditClientCreation(unittest.TestCase):
    """Reddit client setup has one shared credential path."""

    @patch("reddit_stash.praw.Reddit")
    @patch("reddit_stash.load_config_and_env")
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


if __name__ == "__main__":
    unittest.main()
