"""Fixtures for tests of the media download manager."""

from types import SimpleNamespace
from unittest.mock import Mock, patch

from utils.media_services import download_manager as manager_module
from utils.media_services.download_manager import MediaDownloadManager
from utils.media_services.media_types import DownloadResult, DownloadStatus
from utils.media_services.retry_queue import SQLiteRetryQueue

URL = 'https://example.com/image.jpg'


class FakeResponse:
    """A successful streaming response, as _download_with_retry consumes it."""

    status_code = 200

    def __init__(self, body, content_type='video/mp4', error=None):
        self.body = body
        self.headers = {'content-type': content_type}
        self.error = error

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def raise_for_status(self):
        pass

    def iter_content(self, chunk_size):
        yield self.body
        if self.error:
            raise self.error


def failed_download(status=DownloadStatus.FAILED, message='boom'):
    return DownloadResult(status=status, error_message=message)


def media_manager(retry_queue=None):
    """A real manager with media enabled, built with fake collaborators and no downloaders yet."""
    config = Mock(**{'is_media_enabled.return_value': False})  # keeps __init__ from starting services
    with patch.object(manager_module, 'get_media_config', return_value=config), \
            patch.object(manager_module, 'get_retry_queue',
                         return_value=retry_queue or Mock(spec=SQLiteRetryQueue)), \
            patch.object(manager_module, 'ContentRecoveryService',
                         return_value=Mock(**{'is_enabled.return_value': False})):
        manager = MediaDownloadManager()
    config.is_media_enabled.return_value = True
    return manager


def no_transform(url):
    return SimpleNamespace(url=url, transformed=False, platform='', notes='')
