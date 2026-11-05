"""Tests for utils/save_utils.py — save_submission, write_comments_markdown, save_comment_and_context."""

import io
import os
import json
import tempfile
import unittest
import warnings
from pathlib import Path
from unittest.mock import Mock, patch
from praw.models import Comment
from praw.models.comment_forest import CommentForest

from tests.helpers import mock_submission
from utils.media_services.media_types import DownloadResult, DownloadStatus, GalleryImage
from utils.praw_helpers import lazy_load_comments


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

        sub = mock_submission(is_self=True, selftext='Hello world body text')
        with open(self.filepath, 'w') as f:
            save_submission(sub, f)

        content = open(self.filepath).read()
        self.assertIn('Hello world body text', content)

    @patch('utils.save_utils.lazy_load_comments', return_value=[])
    @patch('utils.save_utils.get_media_config')
    def test_submission_frontmatter_quotes_special_title_characters(self, mock_config, mock_comments):
        """Saved posts start with YAML-safe metadata and preserve the original title."""
        from utils.save_utils import save_submission

        title = 'A "quoted": title\nwith a second line'
        sub = mock_submission(is_self=True, selftext='Hello world body text', title=title)
        with open(self.filepath, 'w', encoding='utf-8') as file:
            save_submission(sub, file)

        content = Path(self.filepath).read_text(encoding='utf-8')
        _, frontmatter, _ = content.split('---\n', 2)
        fields = {
            key: json.loads(value)
            for key, value in (line.split(': ', 1) for line in frontmatter.strip().splitlines())
        }
        self.assertEqual(fields['title'], title)
        self.assertEqual(fields['subreddit'], '/r/test')

    def test_frontmatter_preserves_boolean_and_number_scalars(self):
        from io import StringIO

        from utils.save_utils import yaml_frontmatter

        output = StringIO(yaml_frontmatter({'recovered': True, 'score': 42}))
        _, frontmatter, _ = output.getvalue().split('---\n', 2)
        fields = {
            key: json.loads(value)
            for key, value in (line.split(': ', 1) for line in frontmatter.strip().splitlines())
        }

        self.assertIs(fields['recovered'], True)
        self.assertEqual(fields['score'], 42)

    def test_comment_frontmatter_has_a_quoted_title(self):
        """Saved comments use valid frontmatter instead of prose inside YAML delimiters."""
        from utils.save_utils import save_comment_and_context

        comment = _make_comment()
        with open(self.filepath, 'w', encoding='utf-8') as file:
            save_comment_and_context(comment, file)

        content = Path(self.filepath).read_text(encoding='utf-8')
        _, frontmatter, _ = content.split('---\n', 2)
        fields = {
            key: json.loads(value)
            for key, value in (line.split(': ', 1) for line in frontmatter.strip().splitlines())
        }
        self.assertEqual(fields['title'], 'Comment by /u/commenter')
        self.assertEqual(fields['body'], 'Test comment body')

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

        sub = mock_submission(is_self=False, selftext='', url='https://example.com/article')
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

        sub = mock_submission(
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
    @patch('utils.save_utils.download_media_into', return_value=DownloadResult(status=DownloadStatus.FAILED))
    def test_link_post_with_selftext_image(self, mock_dl, mock_config, mock_comments):
        """Link posts with selftext and an image URL should include both."""
        from utils.save_utils import save_submission

        media_cfg = Mock()
        media_cfg.is_albums_enabled.return_value = False
        media_cfg.is_videos_enabled.return_value = False
        media_cfg.is_images_enabled.return_value = True
        mock_config.return_value = media_cfg

        sub = mock_submission(
            is_self=False,
            selftext='Check out this cool image!',
            url='https://i.redd.it/abc123.jpg'
        )
        with open(self.filepath, 'w') as f:
            save_submission(sub, f)

        content = open(self.filepath).read()
        self.assertIn('Check out this cool image!', content)
        self.assertIn('i.redd.it/abc123.jpg', content)


class TestFormatDate(unittest.TestCase):
    def test_utc_output_without_deprecation_warning(self):
        from utils.save_utils import format_date

        with warnings.catch_warnings():
            warnings.simplefilter('error', DeprecationWarning)
            self.assertEqual(format_date(0), '1970-01-01 00:00:00')


class TestExtractVideoId(unittest.TestCase):
    def test_returns_only_the_id(self):
        from utils.save_utils import extract_video_id

        for url in ('https://www.youtube.com/watch?v=abc_-1',
                    'https://youtube.com/watch?v=abc_-1&t=10',
                    'https://m.youtube.com/watch?t=10&v=abc_-1',
                    'https://www.youtube.com/shorts/abc_-1?feature=share',
                    'https://www.youtube.com/embed/abc_-1',
                    'https://youtu.be/abc_-1?t=5'):
            with self.subTest(url=url):
                self.assertEqual(extract_video_id(url), 'abc_-1')

    def test_urls_naming_no_video_return_none(self):
        from utils.save_utils import extract_video_id

        for url in (None, '', 'https://notyoutube.com/watch?v=abc', 'https://example.com/youtube.com/x',
                    'https://www.youtube.com/@channel', 'https://youtu.be/', 'http://[bad',
                    'https://www.youtube.com/watch?v=a)b'):
            with self.subTest(url=url):
                self.assertIsNone(extract_video_id(url))


class TestLinkPostUrlDetection(unittest.TestCase):
    def _body(self, url):
        from utils.save_utils import _save_submission_media

        out = io.StringIO()
        media_config = Mock()
        media_config.is_images_enabled.return_value = False
        _save_submission_media(mock_submission(is_self=False, url=url, is_gallery=False),
                               out, media_config, '/unused', False)
        return out.getvalue()

    def test_youtube_with_trailing_query_gets_a_valid_thumbnail(self):
        url = 'https://www.youtube.com/watch?v=abc&t=10'
        self.assertEqual(self._body(url), f'[![Video](https://img.youtube.com/vi/abc/0.jpg)]({url})')

    def test_youtube_url_with_no_video_is_a_plain_link(self):
        url = 'https://www.youtube.com/@channel'
        self.assertEqual(self._body(url), url)

    def test_missing_url_is_marked_deleted(self):
        self.assertEqual(self._body(None), '[Deleted Post]')

    def test_hosts_that_only_look_like_image_files_are_not_images(self):
        from utils.save_utils import _is_image_url

        self.assertFalse(_is_image_url('https://example.png'))
        self.assertFalse(_is_image_url('https://example.com/page#a.png'))
        self.assertTrue(_is_image_url('https://example.com/a.PNG?w=1'))
        self.assertTrue(_is_image_url('https://i.redd.it/abc'))


class TestWriteCommentsMarkdown(unittest.TestCase):
    """Tests for write_comments_markdown()."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.filepath = os.path.join(self.tmpdir, 'test_comments.md')

    def test_nested_submission_comments_are_each_written_once(self):
        """A real CommentForest: list() flattens it, which must not combine with reply recursion."""
        from utils.save_utils import write_comments_markdown

        submission = Mock()
        grandchild = _make_comment(body='third-body')
        grandchild.replies = CommentForest(submission, [])
        child = _make_comment(body='second-body')
        child.replies = CommentForest(submission, [grandchild])
        parent = _make_comment(body='first-body')
        parent.replies = CommentForest(submission, [child])
        submission.comments = CommentForest(submission, [parent])

        with open(self.filepath, 'w') as f:
            write_comments_markdown(lazy_load_comments(submission), f)

        with open(self.filepath) as f:
            content = f.read()
        for body in ('first-body', 'second-body', 'third-body'):
            self.assertEqual(content.count(body), 1, body)
        self.assertIn('> > third-body', content)

    def test_top_level_comment_no_prefix(self):
        """Top-level comments (depth=0) should have no blockquote prefix."""
        from utils.save_utils import write_comments_markdown

        comment = _make_comment(body='Top level comment')
        with open(self.filepath, 'w') as f:
            write_comments_markdown([comment], f, depth=0)

        content = open(self.filepath).read()
        self.assertIn('Top level comment', content)
        # Lines should not start with >
        for line in content.split('\n'):
            if 'Top level comment' in line:
                self.assertFalse(line.startswith('>'), f"Unexpected blockquote: {line}")

    def test_nested_comment_uses_blockquotes(self):
        """Nested comments (depth=1) should use > prefix."""
        from utils.save_utils import write_comments_markdown

        comment = _make_comment(body='Nested reply')
        with open(self.filepath, 'w') as f:
            write_comments_markdown([comment], f, depth=1)

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
        from utils.save_utils import write_comments_markdown

        comment = _make_comment(body='Some comment')
        child = _make_comment(body='Child reply')
        comment.replies = [child]

        with open(self.filepath, 'w') as f:
            write_comments_markdown([comment], f, depth=0)

        content = open(self.filepath).read()
        box_chars = ['├', '└', '│', '─', '┌', '┐', '┘', '┤', '┬', '┴', '┼']
        for char in box_chars:
            self.assertNotIn(char, content, f"Found box-drawing char {char!r} in output")

    def test_deleted_author_handled(self):
        """Comments with author=None should render as [deleted]."""
        from utils.save_utils import write_comments_markdown

        comment = _make_comment(body='Ghost comment', author_name=None)
        with open(self.filepath, 'w') as f:
            write_comments_markdown([comment], f, depth=0)

        content = open(self.filepath).read()
        self.assertIn('[deleted]', content)

    def test_none_body_handled(self):
        """Comments with body=None should render as [deleted]."""
        from utils.save_utils import write_comments_markdown

        comment = _make_comment(body=None)
        with open(self.filepath, 'w') as f:
            write_comments_markdown([comment], f, depth=0)

        content = open(self.filepath).read()
        self.assertIn('[deleted]', content)

    def test_double_nested_blockquotes(self):
        """Depth=2 comments should use >> prefix."""
        from utils.save_utils import write_comments_markdown

        comment = _make_comment(body='Deep reply')
        with open(self.filepath, 'w') as f:
            write_comments_markdown([comment], f, depth=2)

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

        parent_sub = mock_submission(
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

        parent_sub = mock_submission(
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


class TestFallbackBranches(unittest.TestCase):
    """Branches where saving must carry on with a link or a warning."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.filepath = os.path.join(tmp.name, 'test_post.md')

    def _save(self, sub, **kwargs):
        from utils.save_utils import save_submission

        with open(self.filepath, 'w') as f:
            save_submission(sub, f, **kwargs)
        with open(self.filepath) as f:
            return f.read()

    @patch('utils.save_utils.lazy_load_comments', return_value=[])
    @patch('utils.save_utils.get_media_config')
    @patch('utils.save_utils.download_media_into', side_effect=[
        DownloadResult(status=DownloadStatus.SUCCESS, local_path='/tmp/a.jpg', bytes_downloaded=1),
        DownloadResult(status=DownloadStatus.FAILED),
    ])
    @patch('utils.save_utils.RedditMediaDownloader.extract_media_urls_from_submission')
    def test_context_mode_links_gallery_images_it_never_reached(self, mock_extract, mock_dl, mock_config, _):
        mock_config.return_value.max_concurrent_downloads.return_value = 3
        mock_extract.return_value = [
            GalleryImage(url=f'https://i.redd.it/{name}.jpg', gallery_id=name)
            for name in 'abc'
        ]

        content = self._save(mock_submission(is_self=False, is_gallery=True, url='https://reddit.com/gallery/abc'),
                             context_mode=True)

        self.assertEqual(mock_dl.call_count, 2)
        self.assertIn('![Gallery Image 1](/tmp/a.jpg)', content)
        self.assertIn('![Gallery Image 2](https://i.redd.it/b.jpg)', content)
        self.assertIn('![Gallery Image 3](https://i.redd.it/c.jpg)', content)

    @patch('utils.save_utils.lazy_load_comments', return_value=[])
    @patch('utils.save_utils.get_media_config')
    @patch('utils.save_utils._download_gallery', return_value={})
    @patch('utils.save_utils.RedditMediaDownloader.extract_media_urls_from_submission')
    def test_gallery_uses_the_configured_worker_count(self, mock_extract, mock_download, mock_config, _):
        mock_config.return_value.max_concurrent_downloads.return_value = 7
        mock_extract.return_value = [GalleryImage(url='https://i.redd.it/a.jpg', gallery_id='a')]

        self._save(mock_submission(is_self=False, is_gallery=True, url='https://reddit.com/gallery/abc'))

        self.assertEqual(mock_download.call_args.args[3], 7)

    @patch('utils.save_utils.lazy_load_comments', return_value=[])
    @patch('utils.save_utils.get_media_config')
    @patch('utils.save_utils._save_submission_media', side_effect=RuntimeError('boom'))
    def test_media_error_falls_back_only_in_context_mode(self, _media, _config, _comments):
        sub = mock_submission(is_self=False, url='https://i.redd.it/abc.jpg')

        self.assertIn('**Media:** [Link](https://i.redd.it/abc.jpg)', self._save(sub, context_mode=True))
        with self.assertRaisesRegex(RuntimeError, 'boom'):
            self._save(sub)


class TestMediaBytesAreReturned(unittest.TestCase):
    """Media bytes travel up through return values, from every level that downloads."""

    def _saved(self, nbytes):
        return DownloadResult(status=DownloadStatus.SUCCESS, local_path='/saved/a.png', bytes_downloaded=nbytes)

    def test_comment_tree_sums_each_downloaded_image(self):
        import utils.save_utils as save_utils

        def comment(body, replies=()):
            item = Mock(spec=Comment)
            item.author = Mock()
            item.author.name = 'a'
            item.score = 1
            item.permalink = '/x'
            item.id = 'c'
            item.body = body
            item.replies = list(replies)
            return item

        tree = comment('https://example.com/a.png', [comment('https://example.com/b.png'), comment('text')])
        output = io.StringIO()
        output.name = '/saved/c.md'
        with patch.object(save_utils, 'get_media_config') as config, \
                patch.object(save_utils, 'download_media_into', side_effect=[self._saved(5), self._saved(7)]):
            config.return_value.is_images_enabled.return_value = True
            self.assertEqual(save_utils.write_comments_markdown([tree], output), 12)

    def test_save_submission_adds_comment_bytes_to_the_link_body_bytes(self):
        import utils.save_utils as save_utils

        with patch.object(save_utils, 'get_media_config'), \
                patch.object(save_utils, 'lazy_load_comments', return_value=[]), \
                patch.object(save_utils, '_write_link_post_body', return_value=4), \
                patch.object(save_utils, 'write_comments_markdown', return_value=9):
            self.assertEqual(save_utils.save_submission(mock_submission(is_self=False), io.StringIO()), 13)

    def test_saved_comment_adds_parent_context_bytes_to_its_reply_bytes(self):
        import utils.save_utils as save_utils

        comment = _make_comment(replies=[Mock()])
        comment.parent.return_value = mock_submission()
        with patch.object(save_utils, 'save_submission', return_value=11), \
                patch.object(save_utils, 'write_comments_markdown', return_value=5):
            self.assertEqual(save_utils.save_comment_and_context(comment, io.StringIO()), 16)

    def test_save_to_file_reports_what_the_save_function_returned(self):
        from utils.file_operations import SaveContext, SaveStatus, save_to_file

        item = Mock(id='abc')
        item.subreddit.display_name = 'sub'
        with tempfile.TemporaryDirectory() as d:
            ctx = SaveContext(d, set(), {}, set())
            status, media_size = save_to_file(item, os.path.join(d, 'sub', 'POST_abc.md'), lambda *a, **k: 42, ctx)

        self.assertEqual((status, media_size), (SaveStatus.SAVED, 42))


if __name__ == '__main__':
    unittest.main()
