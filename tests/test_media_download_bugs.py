"""Regression tests for media download failure handling and the retry row lifecycle."""

import io
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch


import reddit_stash
from tests.media_helpers import URL, failed_download, media_manager, no_transform
from utils import save_utils
from utils.media_services import download_manager as manager_module
from utils.file_operations import activity_save_context
from utils.media_services.download_manager import MediaDownloadManager
from utils.media_services.base_downloader import BaseHTTPDownloader
from utils.media_services.imgur_media import ImgurMediaDownloader
from utils.media_services.reddit_media import RedditMediaDownloader
from utils.media_services.media_types import DownloadResult, DownloadStatus, GalleryImage
from utils.media_services.url_security import URLSecurityValidator, ValidationResult

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class TestSecurityRejection(unittest.TestCase):
    def _fetch_with(self, validation):
        manager = media_manager()
        manager._recovery_service.is_enabled.return_value = True
        manager._recovery_service.attempt_recovery.return_value = SimpleNamespace(
            success=False, recovered_url=None, error_message='nothing',
        )
        validator = Mock()
        validator.validate_url.return_value = validation
        with patch.object(manager_module, 'get_url_validator', return_value=validator), \
                patch.object(manager_module.url_transformer, 'transform', no_transform):
            self.assertIsNone(manager.download_media(URL, '/x.jpg').local_path)
        return manager

    def test_rejected_url_is_not_recovered_or_queued(self):
        manager = self._fetch_with(ValidationResult(is_valid=False, issues=['blocked'], risk_level='high'))

        manager._recovery_service.attempt_recovery.assert_not_called()
        manager._retry_queue.add_failed_download.assert_not_called()
        self.assertIn(URL, manager._permanent_failures)

    def test_unresolved_host_is_recovered_and_queued_not_rejected(self):
        manager = self._fetch_with(
            ValidationResult(is_valid=False, issues=['no dns'], risk_level='high', dns_failed=True)
        )

        manager._recovery_service.attempt_recovery.assert_called_once()
        manager._retry_queue.add_failed_download.assert_called_once()
        self.assertNotIn(URL, manager._permanent_failures)

    def test_validator_reports_dns_failure_as_high_risk(self):
        validator = URLSecurityValidator()
        with patch('utils.media_services.url_security._resolve_hostname', side_effect=OSError('no dns')):
            result = validator.validate_url('https://dead.example.com/image.jpg')

        self.assertFalse(result.is_valid)
        self.assertEqual(result.risk_level, 'high')
        self.assertTrue(result.dns_failed)


class TestRecoveryDownload(unittest.TestCase):
    def test_exception_path_recovers_through_service_protection(self):
        manager = media_manager()
        manager._recovery_service.is_enabled.return_value = True
        manager._recovery_service.attempt_recovery.return_value = SimpleNamespace(
            success=True, recovered_url='https://web.archive.org/x.jpg', error_message=None,
        )
        downloader = Mock()
        downloader.download.return_value = DownloadResult(status=DownloadStatus.SUCCESS, local_path='/saved.jpg')
        manager._get_service_for_url = Mock(return_value=('generic', downloader))

        with patch.object(manager, '_resolve_download_url',
                          side_effect=[RuntimeError('broken'), 'https://web.archive.org/x.jpg']):
            self.assertEqual(manager.download_media(URL, '/saved.jpg').local_path, '/saved.jpg')

        downloader.download.assert_called_once()
        self.assertIn('generic', manager._breakers)
        manager._retry_queue.add_failed_download.assert_not_called()


class TestMediaFlagGating(unittest.TestCase):
    def test_images_off_does_not_block_other_media(self):
        manager = media_manager()
        manager._media_config.is_images_enabled.return_value = False
        manager._media_config.is_media_enabled.return_value = True
        manager._fetch = Mock(return_value=(
            DownloadResult(status=DownloadStatus.SUCCESS, local_path='/v.mp4'), 'reddit_video',
        ))

        self.assertEqual(manager.download_media('https://v.redd.it/a/DASH_720.mp4', '/v.mp4').local_path, '/v.mp4')

    def test_media_off_blocks_everything(self):
        manager = media_manager()
        manager._media_config.is_media_enabled.return_value = False
        manager._fetch = Mock()

        self.assertIsNone(manager.download_media(URL, '/x.jpg').local_path)
        manager._fetch.assert_not_called()

    def test_comment_image_respects_the_image_flag(self):
        from praw.models import Comment
        from utils.save_utils import write_comments_markdown

        comment = Mock(spec=Comment)
        comment.author = None
        comment.score = 1
        comment.permalink = '/r/x/1'
        comment.body = 'look https://example.com/a.jpg'
        comment.id = 'abc'
        comment.replies = []
        output = io.StringIO()
        output.name = '/tmp/COMMENT_abc.md'

        with patch('utils.save_utils.get_media_config') as config, \
                patch('utils.save_utils.download_media_into') as download:
            config.return_value.is_images_enabled.return_value = False
            write_comments_markdown([comment], output)

        download.assert_not_called()
        self.assertIn('![Image](https://example.com/a.jpg)', output.getvalue())


class TestInitialization(unittest.TestCase):
    def test_importing_the_retry_queue_creates_no_database(self):
        with tempfile.TemporaryDirectory() as cwd:
            subprocess.run(
                [sys.executable, '-c', 'import utils.media_services.retry_queue'],
                cwd=cwd,
                env={**os.environ, 'PYTHONPATH': REPO_ROOT},
                check=True,
            )
            self.assertEqual(os.listdir(cwd), [])

    def test_activity_modes_are_read_when_the_save_runs(self):
        with tempfile.TemporaryDirectory() as d:
            settings = os.path.join(d, 'settings.ini')
            with open(settings, 'w', encoding='utf-8') as f:
                f.write('[Settings]\ncheck_type = BOGUS\n')
            with patch('utils.env_config.SETTINGS_PATH', settings):
                with self.assertRaisesRegex(ValueError, 'BOGUS'):
                    activity_save_context(d, {})

    def test_failed_service_init_leaves_the_generic_downloader_usable(self):
        manager = media_manager()
        with patch.object(manager_module, 'RedditMediaDownloader', side_effect=RuntimeError('broken')):
            manager._initialize_services()

        self.assertIsInstance(manager._generic_downloader, BaseHTTPDownloader)
        self.assertIsNone(manager._get_service_for_url('https://i.redd.it/x.jpg')[1])

    def test_concurrent_first_callers_share_one_manager(self):
        barrier = threading.Barrier(4)
        created = []

        def slow_manager():
            created.append(object())
            time.sleep(0.05)
            return created[-1]

        results = []

        def first_call():
            barrier.wait()
            results.append(manager_module.get_media_manager())

        with patch.object(manager_module, '_media_manager', None), \
                patch.object(manager_module, 'MediaDownloadManager', slow_manager):
            threads = [threading.Thread(target=first_call) for _ in range(4)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

        self.assertEqual(len(created), 1)
        self.assertEqual(len({id(result) for result in results}), 1)

    def test_retry_pass_relinks_recovered_media(self):
        from utils.save_utils import relink_media_line

        media_manager = Mock()
        media_manager.process_pending_retries.return_value = {
            'processed': 0, 'successful': 0, 'failed': 0, 'skipped': 0, 'unlinked': 0,
        }
        with patch('reddit_stash.get_retry_queue') as queue, \
                patch('reddit_stash.get_media_manager', return_value=media_manager):
            queue.return_value.get_pending_retries.return_value = [{'url': URL}]
            reddit_stash.retry_pending_media()

        media_manager.process_pending_retries.assert_called_once_with(on_recovered=relink_media_line)


class TestManagerStartup(unittest.TestCase):
    def test_services_start_when_only_images_are_off(self):
        config = Mock()
        config.is_media_enabled.return_value = True
        config.is_images_enabled.return_value = False
        with patch.object(manager_module, 'get_media_config', return_value=config), \
                patch.object(manager_module, 'get_retry_queue'), \
                patch.object(manager_module, 'ContentRecoveryService'), \
                patch.object(MediaDownloadManager, '_initialize_services') as start:
            MediaDownloadManager()
        start.assert_called_once()


class TestOutcomesSurviveTheLayers(unittest.TestCase):
    def test_disabled_and_failed_downloads_are_distinguishable(self):
        manager = media_manager()
        manager._media_config.is_media_enabled.return_value = False
        self.assertEqual(manager.download_media(URL, '/x.jpg').status, DownloadStatus.SKIPPED)

        manager._media_config.is_media_enabled.return_value = True
        manager._fetch = Mock(return_value=(failed_download(DownloadStatus.FORBIDDEN), 'generic'))
        result = manager.download_media(URL, '/x.jpg')

        self.assertEqual((result.status, result.error_message), (DownloadStatus.FORBIDDEN, 'boom'))
        manager._retry_queue.add_failed_download.assert_called_once()

    def test_repeated_url_reports_the_size_of_its_file(self):
        manager = media_manager()
        with tempfile.NamedTemporaryFile() as saved:
            saved.write(b'12345')
            saved.flush()
            manager._downloaded_urls[URL] = saved.name

            result = manager.download_media(URL, saved.name)

        self.assertEqual((result.local_path, result.bytes_downloaded), (saved.name, 5))

    def test_a_broken_manager_ends_as_a_failed_result(self):
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(manager_module, 'get_media_manager', side_effect=RuntimeError('no database')):
            result = manager_module.download_media_into(URL, folder, 'abc')

        self.assertEqual((result.status, result.error_message), (DownloadStatus.FAILED, 'no database'))

    def test_media_size_counts_the_bytes_of_a_whole_album(self):
        media_config = Mock()
        media_config.is_images_enabled.return_value = True
        submission = Mock(url='https://imgur.com/a/abc', id='abc')
        album = DownloadResult(status=DownloadStatus.SUCCESS, local_path='/saved/abc_album', bytes_downloaded=1234)

        with patch.object(save_utils, 'download_media_into', return_value=album):
            size = save_utils._save_image(submission, io.StringIO(), media_config, '/saved')

        self.assertEqual(size, 1234)

    def test_repeated_album_url_reports_the_bytes_of_its_images(self):
        manager = media_manager()
        with tempfile.TemporaryDirectory() as album:
            for name, content in (('image_001.jpg', b'12345'), ('image_002.jpg', b'678')):
                with open(os.path.join(album, name), 'wb') as saved:
                    saved.write(content)
            manager._downloaded_urls[URL] = album

            result = manager.download_media(URL, album)

        self.assertEqual((result.local_path, result.bytes_downloaded), (album, 8))

    def test_imgur_album_reports_the_bytes_of_the_images_it_saved(self):
        downloader = ImgurMediaDownloader()
        downloader._client_ids = ['id']
        response = MagicMock(status_code=200)
        response.json.return_value = {'success': True, 'data': {
            'images': [{'link': f'https://i.imgur.com/{i}.jpg'} for i in range(3)],
        }}
        images = [DownloadResult(status=DownloadStatus.SUCCESS, local_path='/1.jpg', bytes_downloaded=10),
                  failed_download(DownloadStatus.NOT_FOUND),
                  DownloadResult(status=DownloadStatus.SUCCESS, local_path='/3.jpg', bytes_downloaded=5)]
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(downloader, '_api_get', return_value=(response, None)), \
                patch.object(downloader, 'download_file', side_effect=images):
            result = downloader.download('https://imgur.com/a/abc', os.path.join(folder, 'a.jpg'))

        self.assertEqual((result.status, result.bytes_downloaded), (DownloadStatus.SUCCESS, 15))

    def test_album_images_can_rely_on_download_file_not_raising(self):
        downloader = ImgurMediaDownloader()
        for error in (RuntimeError('bug'), KeyError('bug')):
            with self.subTest(error=error), \
                    patch.object(downloader, '_download_with_retry', side_effect=error):
                self.assertEqual(downloader.download_file(URL, '/x.jpg').status, DownloadStatus.FAILED)

    def _media_size_after(self, save, saved_bytes=7):
        """Media size counted by a save path whose download succeeds with saved_bytes."""
        saved = DownloadResult(status=DownloadStatus.SUCCESS, local_path='/saved/a.jpg', bytes_downloaded=saved_bytes)
        with patch.object(save_utils, 'download_media_into', return_value=saved):
            return save()

    def test_gallery_counts_the_bytes_of_each_image(self):
        submission = Mock(id='abc', permalink='/r/x/abc')
        media_config = Mock()
        media_config.max_concurrent_downloads.return_value = 2
        images = [GalleryImage(url=URL, gallery_id=str(i)) for i in range(2)]
        with patch.object(save_utils.RedditMediaDownloader, 'extract_media_urls_from_submission', return_value=images):
            size = self._media_size_after(
                lambda: save_utils._save_gallery(submission, io.StringIO(), media_config, '/saved', False))

        self.assertEqual(size, 14)

    def test_video_counts_its_bytes(self):
        submission = Mock(id='abc', url='https://v.redd.it/abc', media=None)
        media_config = Mock()
        media_config.is_videos_enabled.return_value = True

        size = self._media_size_after(lambda: save_utils._save_video(submission, io.StringIO(), media_config, '/saved'))

        self.assertEqual(size, 7)

    def test_comment_image_counts_its_bytes(self):
        output = io.StringIO()
        output.name = '/saved/c.md'
        with patch.object(save_utils, 'get_media_config') as media_config:
            media_config.return_value.is_images_enabled.return_value = True
            size = self._media_size_after(
                lambda: save_utils._write_comment_body(Mock(id='c'), 'https://example.com/a.png', output, ''))

        self.assertEqual(size, 7)


class TestFrozenExtensionLists(unittest.TestCase):
    """These lists decide saved file names and which saved comments render an image."""

    def test_saved_file_names_keep_only_the_known_extensions(self):
        cases = {
            '.jpg': '.jpg', '.jpeg': '.jpeg', '.png': '.png', '.gif': '.gif', '.webp': '.webp',
            '.mp4': '.mp4', '.webm': '.webm',
            '.bmp': '.jpg', '.tiff': '.jpg', '.mov': '.jpg', '.mp3': '.jpg', '.xyz': '.jpg',
        }
        for extension, saved_as in cases.items():
            with self.subTest(extension=extension), tempfile.TemporaryDirectory() as folder, \
                    patch.object(manager_module, 'get_media_manager') as get_manager:
                manager_module.download_media_into(f'https://example.com/a{extension}', folder, 'abc')
                get_manager.return_value.download_media.assert_called_once_with(
                    f'https://example.com/a{extension}', os.path.join(folder, f'abc{saved_as}'))

    def test_only_these_comment_endings_download_an_image(self):
        for extension in ('.jpg', '.jpeg', '.png', '.gif'):
            self.assertEqual(save_utils._trailing_image_url(f'see https://x.co/a{extension}'),
                             f'https://x.co/a{extension}')
        for extension in ('.webp', '.bmp', '.tiff', '.mp4'):
            self.assertIsNone(save_utils._trailing_image_url(f'see https://x.co/a{extension}'))


class TestSingleCatchBoundaries(unittest.TestCase):
    def test_imgur_api_error_still_falls_back_to_direct_download(self):
        downloader = ImgurMediaDownloader()
        downloader._client_ids = ['id']
        direct = DownloadResult(status=DownloadStatus.SUCCESS, local_path='/x.jpg')
        with patch.object(downloader, '_api_get', side_effect=ConnectionError('api down')), \
                patch.object(downloader, '_download_image_direct', return_value=direct):
            self.assertIs(downloader.download('https://imgur.com/abc', '/x.jpg'), direct)

    def test_malformed_urls_are_refused_by_the_url_predicates(self):
        bad_url = 'http://[bad/x.jpg'
        self.assertFalse(ImgurMediaDownloader().can_handle(bad_url))
        self.assertFalse(RedditMediaDownloader().can_handle(bad_url))
        self.assertFalse(save_utils._is_image_url(bad_url))
        self.assertFalse(save_utils._is_video_url(bad_url))

    def test_url_predicates_do_not_hide_programming_errors(self):
        with patch.object(save_utils, 'urlparse', side_effect=TypeError('bug')), self.assertRaises(TypeError):
            save_utils._is_image_url(URL)

    def test_routing_falls_back_to_generic_on_a_malformed_url(self):
        manager = media_manager()
        manager._generic_downloader = Mock()
        self.assertEqual(manager._get_service_for_url('http://[bad/x.jpg'), ('generic', manager._generic_downloader))

    def test_recovered_url_follows_the_same_rules_as_a_first_attempt(self):
        manager = media_manager()
        recovered = 'https://i.redd.it/recovered.jpg'
        manager._recovery_service.is_enabled.return_value = True
        manager._recovery_service.attempt_recovery.return_value = SimpleNamespace(
            success=True, recovered_url=recovered, error_message=None,
        )
        downloader = Mock()
        downloader.download.return_value = DownloadResult(status=DownloadStatus.SUCCESS, local_path='/x.jpg')
        manager._get_service_for_url = Mock(return_value=('reddit_image', downloader))
        validator = Mock()

        with patch.object(manager_module, 'get_url_validator', return_value=validator), \
                patch.object(manager_module.url_transformer, 'transform', no_transform):
            result = manager._try_recovery(URL, 'gone', '/x.jpg')

        self.assertEqual(result.local_path, '/x.jpg')
        downloader.download.assert_called_once_with(recovered, '/x.jpg')
        validator.validate_url.assert_not_called()  # trusted media hosts skip it, as in _resolve_download_url


if __name__ == '__main__':
    unittest.main()
