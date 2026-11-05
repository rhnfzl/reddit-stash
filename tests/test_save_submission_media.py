"""What save_submission writes for each kind of link post, and what it asks to download."""

import os
import tempfile
import unittest
from unittest.mock import Mock, call, patch

from tests.helpers import mock_submission
from utils.media_services.media_types import DownloadResult, DownloadStatus
from utils.save_utils import save_submission

STREAM = 'https://v.redd.it/vid/DASH_720.mp4?source=fallback'
VIDEO = 'https://v.redd.it/vid'


def _saved(path, size=5):
    return DownloadResult(status=DownloadStatus.SUCCESS, local_path=path, bytes_downloaded=size)


FAILED = DownloadResult(status=DownloadStatus.FAILED, error_message='boom')


class TestLinkPostMedia(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name
        self.config = Mock(**{'max_concurrent_downloads.return_value': 2})
        for patcher in (
            patch('utils.save_utils.get_media_config', return_value=self.config),
            patch('utils.save_utils.download_media_into', return_value=FAILED),
        ):
            mocked = patcher.start()
            self.addCleanup(patcher.stop)
        self.download = mocked

    def _save(self, post, **kwargs):
        path = os.path.join(self.dir, 'post.md')
        with open(path, 'w', encoding='utf-8') as f:
            size = save_submission(post, f, **kwargs)
        with open(path, encoding='utf-8') as f:
            return size, f.read()

    def _video_post(self):
        post = mock_submission(is_self=False, url=VIDEO)
        post.media = {'reddit_video': {'fallback_url': STREAM}}
        return post

    def _gallery_post(self):
        post = mock_submission(is_self=False, url='https://www.reddit.com/gallery/abc123', is_gallery=True)
        post.media_metadata = {
            'one': {'s': {'u': 'https://preview.redd.it/one.jpg?a=1&amp;b=2'}},
            'two': {'s': {'u': 'https://preview.redd.it/two.jpg'}},
            'skipped': {'status': 'failed'},
        }
        return post

    def test_video_download_links_the_file_and_the_original(self):
        self.download.return_value = _saved('/saves/abc123.mp4', 9)

        size, markdown = self._save(self._video_post())

        self.download.assert_called_once_with(STREAM, self.dir, 'abc123')
        self.assertEqual(size, 9)
        self.assertIn('**Video:** [abc123.mp4](/saves/abc123.mp4)\n', markdown)
        self.assertIn(f'**Original Video URL:** [Link]({VIDEO})\n', markdown)

    def test_failed_video_links_the_post_url(self):
        size, markdown = self._save(self._video_post())

        self.assertEqual(size, 0)
        self.assertIn(f'**Video:** [Link]({VIDEO})\n', markdown)
        self.assertNotIn('Original Video URL', markdown)

    def test_disabled_videos_are_not_downloaded(self):
        self.config.is_videos_enabled.return_value = False

        size, markdown = self._save(self._video_post())

        self.download.assert_not_called()
        self.assertEqual(size, 0)
        self.assertIn(f'**Video (download disabled):** [Link]({VIDEO})\n', markdown)

    def test_youtube_links_embed_a_thumbnail_without_downloading(self):
        for url in ('https://www.youtube.com/watch?v=dQw4w9WgXcQ', 'https://youtu.be/dQw4w9WgXcQ'):
            with self.subTest(url=url):
                size, markdown = self._save(mock_submission(is_self=False, url=url))

                self.download.assert_not_called()
                self.assertEqual(size, 0)
                self.assertIn(f'[![Video](https://img.youtube.com/vi/dQw4w9WgXcQ/0.jpg)]({url})', markdown)

    def test_other_links_are_written_as_they_are(self):
        url = 'https://example.com/article'

        size, markdown = self._save(mock_submission(is_self=False, url=url))

        self.download.assert_not_called()
        self.assertEqual(size, 0)
        self.assertTrue(markdown.endswith(url) or f'{url}\n' in markdown)

    def test_gallery_tries_every_image_and_links_those_that_failed(self):
        first = 'https://preview.redd.it/one.jpg?a=1&b=2'
        second = 'https://preview.redd.it/two.jpg'
        self.download.side_effect = lambda url, *_: FAILED if url == first else _saved('/saves/abc123_two.jpg', 4)

        size, markdown = self._save(self._gallery_post())

        self.assertCountEqual(self.download.call_args_list, [
            call(first, self.dir, 'abc123_one'),
            call(second, self.dir, 'abc123_two'),
        ])
        self.assertEqual(size, 4)
        self.assertIn('**Gallery (2 images)**', markdown)
        self.assertIn(f'![Gallery Image 1]({first})\n*Image 1 of 2*', markdown)
        self.assertIn('![Gallery Image 2](/saves/abc123_two.jpg)\n*Image 2 of 2*', markdown)

    def test_gallery_with_no_usable_images_links_the_post(self):
        post = mock_submission(is_self=False, url='https://www.reddit.com/gallery/abc123', is_gallery=True)
        post.media_metadata = {'skipped': {'status': 'failed'}}

        _, markdown = self._save(post)

        self.download.assert_not_called()
        self.assertIn('**Gallery post** (images unavailable)', markdown)

    def test_disabled_albums_fall_through_to_the_plain_url(self):
        self.config.is_albums_enabled.return_value = False

        _, markdown = self._save(self._gallery_post())

        self.download.assert_not_called()
        self.assertNotIn('Gallery', markdown)
        self.assertIn('https://www.reddit.com/gallery/abc123', markdown)


if __name__ == '__main__':
    unittest.main()
