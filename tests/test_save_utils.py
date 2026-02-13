"""Tests for utils/save_utils.py — save_submission, process_comments, save_comment_and_context."""

import os
import tempfile
import unittest
from unittest.mock import Mock, patch
from praw.models import Submission, Comment


def _make_submission(is_self=True, selftext='', url='', title='Test Title',
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

    # lazy_load_comments returns an empty list by default
    sub.comments = Mock()
    sub.comments.list.return_value = []

    return sub


def _make_comment(body='Test comment body', author_name='commenter',
                  score=10, permalink='/r/test/comments/abc/test_title/def456/',
                  replies=None):
    """Create a mock PRAW Comment that passes isinstance(comment, Comment)."""
    comment = Mock(spec=Comment)
    comment.id = 'def456'
    comment.body = body
    comment.score = score
    comment.permalink = permalink
    comment.replies = replies or []

    if author_name:
        author = Mock()
        author.name = author_name
        comment.author = author
    else:
        comment.author = None

    return comment


class TestSaveSubmission(unittest.TestCase):
    """Tests for save_submission()."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.filepath = os.path.join(self.tmpdir, 'test_post.md')

    @patch('utils.save_utils.lazy_load_comments', return_value=[])
    @patch('utils.save_utils.get_media_config')
    def test_self_post_writes_selftext(self, mock_config, mock_comments):
        """Self posts should write selftext normally."""
        from utils.save_utils import save_submission

        sub = _make_submission(is_self=True, selftext='Hello world body text')
        with open(self.filepath, 'w') as f:
            save_submission(sub, f)

        content = open(self.filepath).read()
        self.assertIn('Hello world body text', content)

    @patch('utils.save_utils.lazy_load_comments', return_value=[])
    @patch('utils.save_utils.get_media_config')
    def test_link_post_no_selftext(self, mock_config, mock_comments):
        """Link posts without selftext should just write the URL."""
        from utils.save_utils import save_submission

        media_cfg = Mock()
        media_cfg.is_albums_enabled.return_value = False
        media_cfg.is_videos_enabled.return_value = False
        media_cfg.is_images_enabled.return_value = False
        mock_config.return_value = media_cfg

        sub = _make_submission(is_self=False, selftext='', url='https://example.com/article')
        with open(self.filepath, 'w') as f:
            save_submission(sub, f)

        content = open(self.filepath).read()
        self.assertIn('https://example.com/article', content)
        # Should NOT have the separator since there's no selftext
        self.assertNotIn('---\n\nhttps://example.com', content)

    @patch('utils.save_utils.lazy_load_comments', return_value=[])
    @patch('utils.save_utils.get_media_config')
    def test_link_post_with_selftext(self, mock_config, mock_comments):
        """Link posts WITH selftext should include BOTH the body text and the URL."""
        from utils.save_utils import save_submission

        media_cfg = Mock()
        media_cfg.is_albums_enabled.return_value = False
        media_cfg.is_videos_enabled.return_value = False
        media_cfg.is_images_enabled.return_value = False
        mock_config.return_value = media_cfg

        sub = _make_submission(
            is_self=False,
            selftext='This is my detailed analysis of the article.',
            url='https://example.com/article'
        )
        with open(self.filepath, 'w') as f:
            save_submission(sub, f)

        content = open(self.filepath).read()
        self.assertIn('This is my detailed analysis of the article.', content)
        self.assertIn('https://example.com/article', content)
        # Selftext should appear before the URL, separated by ---
        selftext_pos = content.index('This is my detailed analysis')
        url_pos = content.index('https://example.com/article')
        self.assertLess(selftext_pos, url_pos)

    @patch('utils.save_utils.lazy_load_comments', return_value=[])
    @patch('utils.save_utils.get_media_config')
    @patch('utils.save_utils.download_image', return_value=(None, 0))
    def test_link_post_with_selftext_image(self, mock_dl, mock_config, mock_comments):
        """Link posts with selftext and an image URL should include both."""
        from utils.save_utils import save_submission

        media_cfg = Mock()
        media_cfg.is_albums_enabled.return_value = False
        media_cfg.is_videos_enabled.return_value = False
        media_cfg.is_images_enabled.return_value = True
        mock_config.return_value = media_cfg

        sub = _make_submission(
            is_self=False,
            selftext='Check out this cool image!',
            url='https://i.redd.it/abc123.jpg'
        )
        with open(self.filepath, 'w') as f:
            save_submission(sub, f)

        content = open(self.filepath).read()
        self.assertIn('Check out this cool image!', content)
        self.assertIn('i.redd.it/abc123.jpg', content)


class TestProcessComments(unittest.TestCase):
    """Tests for process_comments()."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.filepath = os.path.join(self.tmpdir, 'test_comments.md')

    def test_top_level_comment_no_prefix(self):
        """Top-level comments (depth=0) should have no blockquote prefix."""
        from utils.save_utils import process_comments

        comment = _make_comment(body='Top level comment')
        with open(self.filepath, 'w') as f:
            process_comments([comment], f, depth=0)

        content = open(self.filepath).read()
        self.assertIn('Top level comment', content)
        # Lines should not start with >
        for line in content.split('\n'):
            if 'Top level comment' in line:
                self.assertFalse(line.startswith('>'), f"Unexpected blockquote: {line}")

    def test_nested_comment_uses_blockquotes(self):
        """Nested comments (depth=1) should use > prefix."""
        from utils.save_utils import process_comments

        comment = _make_comment(body='Nested reply')
        with open(self.filepath, 'w') as f:
            process_comments([comment], f, depth=1)

        content = open(self.filepath).read()
        self.assertIn('Nested reply', content)
        # Find the line with the body text — it should have > prefix
        found_prefixed = False
        for line in content.split('\n'):
            if 'Nested reply' in line:
                self.assertTrue(line.startswith('> '), f"Missing blockquote: {line!r}")
                found_prefixed = True
        self.assertTrue(found_prefixed, "Did not find 'Nested reply' in output")

    def test_no_box_drawing_characters(self):
        """Output should contain no Unicode box-drawing characters."""
        from utils.save_utils import process_comments

        comment = _make_comment(body='Some comment')
        child = _make_comment(body='Child reply')
        comment.replies = [child]

        with open(self.filepath, 'w') as f:
            process_comments([comment], f, depth=0)

        content = open(self.filepath).read()
        box_chars = ['├', '└', '│', '─', '┌', '┐', '┘', '┤', '┬', '┴', '┼']
        for char in box_chars:
            self.assertNotIn(char, content, f"Found box-drawing char {char!r} in output")

    def test_deleted_author_handled(self):
        """Comments with author=None should render as [deleted]."""
        from utils.save_utils import process_comments

        comment = _make_comment(body='Ghost comment', author_name=None)
        with open(self.filepath, 'w') as f:
            process_comments([comment], f, depth=0)

        content = open(self.filepath).read()
        self.assertIn('[deleted]', content)

    def test_none_body_handled(self):
        """Comments with body=None should render as [deleted]."""
        from utils.save_utils import process_comments

        comment = _make_comment(body=None)
        with open(self.filepath, 'w') as f:
            process_comments([comment], f, depth=0)

        content = open(self.filepath).read()
        self.assertIn('[deleted]', content)

    def test_double_nested_blockquotes(self):
        """Depth=2 comments should use >> prefix."""
        from utils.save_utils import process_comments

        comment = _make_comment(body='Deep reply')
        with open(self.filepath, 'w') as f:
            process_comments([comment], f, depth=2)

        content = open(self.filepath).read()
        found = False
        for line in content.split('\n'):
            if 'Deep reply' in line:
                self.assertTrue(line.startswith('> > '), f"Expected >> prefix: {line!r}")
                found = True
        self.assertTrue(found)


class TestSaveCommentAndContext(unittest.TestCase):
    """Tests for save_comment_and_context()."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.filepath = os.path.join(self.tmpdir, 'test_context.md')

    @patch('utils.save_utils.lazy_load_comments', return_value=[])
    @patch('utils.save_utils.get_media_config')
    def test_link_parent_with_selftext(self, mock_config, mock_comments):
        """When a comment's parent is a link post with selftext, both should appear."""
        from utils.save_utils import save_comment_and_context

        media_cfg = Mock()
        media_cfg.is_albums_enabled.return_value = False
        media_cfg.is_videos_enabled.return_value = False
        media_cfg.is_images_enabled.return_value = False
        mock_config.return_value = media_cfg

        parent_sub = _make_submission(
            is_self=False,
            selftext='Parent body text here.',
            url='https://example.com/linked-article'
        )

        comment = Mock(spec=Comment)
        comment.id = 'com123'
        comment.body = 'My comment on this post'
        comment.score = 5
        comment.permalink = '/r/test/comments/abc/test_title/com123/'
        comment.replies = []
        author = Mock()
        author.name = 'commentauthor'
        comment.author = author
        comment.parent.return_value = parent_sub

        with open(self.filepath, 'w') as f:
            save_comment_and_context(comment, f)

        content = open(self.filepath).read()
        self.assertIn('Parent body text here.', content)
        self.assertIn('https://example.com/linked-article', content)

    @patch('utils.save_utils.lazy_load_comments', return_value=[])
    @patch('utils.save_utils.get_media_config')
    def test_self_parent_still_works(self, mock_config, mock_comments):
        """Self post parents should still work as before."""
        from utils.save_utils import save_comment_and_context

        parent_sub = _make_submission(
            is_self=True,
            selftext='Self post body content.'
        )

        comment = Mock(spec=Comment)
        comment.id = 'com456'
        comment.body = 'Replying to self post'
        comment.score = 3
        comment.permalink = '/r/test/comments/abc/test_title/com456/'
        comment.replies = []
        author = Mock()
        author.name = 'replier'
        comment.author = author
        comment.parent.return_value = parent_sub

        with open(self.filepath, 'w') as f:
            save_comment_and_context(comment, f)

        content = open(self.filepath).read()
        self.assertIn('Self post body content.', content)


if __name__ == '__main__':
    unittest.main()
