"""Fixtures shared by test modules that are not about recovery."""

import importlib.util
import os
import tempfile
import unittest
from unittest.mock import Mock, patch

from praw.models import Comment, Submission

# requirements.txt leaves dropbox out, so its tests only run where requirements-dropbox.txt is installed.
HAS_DROPBOX = importlib.util.find_spec("dropbox") is not None
needs_dropbox = unittest.skipUnless(HAS_DROPBOX, "needs requirements-dropbox.txt")


def _praw_item(praw_class, item_id):
    reddit = Mock()
    reddit.subreddit.return_value.display_name = "python"  # Comment asks reddit for its Subreddit
    item = praw_class(reddit, id=item_id)
    item.subreddit = "python"
    return item


def mock_submission(is_self=True, selftext='', url='', title='Test Title',
                    score=42, num_comments=5, permalink='/r/test/comments/abc/test_title/',
                    flair=None, is_gallery=False, author_name='testuser'):
    """Create a mock PRAW Submission that passes isinstance(sub, Submission)."""
    sub = Mock(spec=Submission)
    sub.id = 'abc123'
    sub.is_self = is_self
    sub.selftext = selftext
    sub.url = url
    sub.title = title
    sub.score = score
    sub.num_comments = num_comments
    sub.permalink = permalink
    sub.link_flair_text = flair
    sub.is_gallery = is_gallery
    sub.created_utc = 1700000000
    sub.media = None
    sub.media_metadata = None

    author = Mock()
    author.name = author_name
    sub.author = author

    subreddit = Mock()
    subreddit.display_name = 'test'
    sub.subreddit = subreddit

    # An empty CommentForest iterates as nothing, so lazy_load_comments yields no comments
    sub.comments = []

    return sub


def praw_submission(item_id):
    return _praw_item(Submission, item_id)


def praw_comment(item_id):
    return _praw_item(Comment, item_id)


def clear_storage_env(test):
    """Hide storage overrides exported in the developer's shell for the length of a test."""
    env_patch = patch.dict(os.environ)
    env_patch.start()
    test.addCleanup(env_patch.stop)
    for name in ('STORAGE_PROVIDER', 'AWS_S3_BUCKET', 'AWS_DEFAULT_REGION', 'S3_STORAGE_CLASS', 'S3_ENDPOINT_URL'):
        os.environ.pop(name, None)


class SettingsFileTestCase(unittest.TestCase):
    """Points the loader at a temp settings.ini and runs from a directory without one."""

    SETTINGS = ""

    def setUp(self):
        clear_storage_env(self)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = tmp.name
        self.settings = os.path.join(self.root, 'settings.ini')
        self.write_settings(self.SETTINGS)

        cwd = os.path.join(self.root, 'elsewhere')
        os.mkdir(cwd)
        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(cwd)

        path_patch = patch('utils.env_config.SETTINGS_PATH', self.settings)
        path_patch.start()
        self.addCleanup(path_patch.stop)

    def write_settings(self, text):
        with open(self.settings, 'w', encoding='utf-8') as f:
            f.write(text)
