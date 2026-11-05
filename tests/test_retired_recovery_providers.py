"""Settings files may still hold retired keys, and those must stay harmless."""

import logging
import os
import subprocess
import sys
import unittest
from unittest.mock import patch

from tests.helpers import SettingsFileTestCase
from utils.config_validator import ConfigValidator
from utils.content_recovery.recovery_config import RecoveryConfig, load_recovery_config
from utils.content_recovery.recovery_metadata import RecoverySource
from utils.content_recovery.recovery_service import ContentRecoveryService
from utils.feature_flags import get_feature_summary, get_media_config

OLD_SETTINGS = """
[Settings]
save_directory = reddit/
[Configuration]
client_id = None
[Media]
download_enabled = true
download_audio = maybe
thumbnail_size = -1
max_image_size = abc
create_thumbnails = maybe
video_quality = ultra
max_video_size = -1
max_album_images = -1
download_timeout = 0
max_daily_storage_mb = 0
[Imgur]
client_ids = abc,def
recover_deleted = maybe
[Recovery]
use_reddit_previews = maybe
use_reveddit_api = maybe
"""


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class TestOldSettingsFile(SettingsFileTestCase):
    SETTINGS = OLD_SETTINGS

    def setUp(self):
        super().setUp()
        get_media_config.cache_clear()
        self.addCleanup(get_media_config.cache_clear)

    def test_retired_and_unread_keys_do_not_fail_validation(self):
        result = ConfigValidator().validate_all()
        self.assertEqual(result['errors'], [])

    def test_retired_recovery_keys_leave_the_defaults_in_place(self):
        self.assertEqual(load_recovery_config(), RecoveryConfig())

    def test_a_settings_file_without_retired_keys_does_not_warn(self):
        self.write_settings('[Recovery]\ntimeout_seconds = 10\n')
        with patch('utils.content_recovery.recovery_service.RecoveryCacheManager'), \
                self.assertNoLogs('utils.content_recovery.recovery_service', logging.WARNING):
            service = ContentRecoveryService()
        self.assertEqual(len(service.providers), 3)

    def test_use_pushshift_api_switches_the_pullpush_provider(self):
        self.write_settings('[Recovery]\nuse_pushshift_api = false\n')
        with patch('utils.content_recovery.recovery_service.RecoveryCacheManager'):
            service = ContentRecoveryService()
        self.assertNotIn(RecoverySource.PULLPUSH_IO, service.providers)
        self.assertEqual(len(service.providers), 2)


class TestLiveMediaSettingsValidation(SettingsFileTestCase):
    """Removing the inert keys must leave the checks on settings the run still reads."""

    def setUp(self):
        super().setUp()
        get_media_config.cache_clear()
        self.addCleanup(get_media_config.cache_clear)

    def _errors_for(self, extra):
        self.write_settings(
            f'[Settings]\nsave_directory = reddit/\n[Configuration]\nclient_id = None\n'
            f'[Media]\ndownload_enabled = true\n{extra}\n'
        )
        get_media_config.cache_clear()
        return ConfigValidator().validate_all()['errors']

    def test_valid_settings_have_no_errors(self):
        self.assertEqual(self._errors_for(''), [])

    def test_recovery_limits_are_not_checked_when_media_is_disabled(self):
        self.write_settings(
            '[Settings]\nsave_directory = reddit/\n[Configuration]\nclient_id = None\n'
            '[Media]\ndownload_enabled = false\n[Recovery]\ntimeout_seconds = 0\n'
        )
        self.assertEqual(ConfigValidator().validate_all()['errors'], [])

    def test_summary_lists_only_the_media_types_the_run_reads(self):
        self.write_settings(
            '[Media]\ndownload_enabled = true\ndownload_audio = true\ndownload_thumbnails = true\n'
        )
        self.assertEqual(get_feature_summary(), 'Media downloads: ENABLED (images, videos, albums)')

    def test_malformed_live_settings_fail_validation(self):
        for extra in ('download_images = maybe', 'download_videos = maybe', 'download_albums = maybe',
                      'max_concurrent_downloads = 0',
                      '[Recovery]\ntimeout_seconds = 0', '[Recovery]\ncache_duration_hours = 0',
                      '[Recovery]\nuse_pushshift_api = maybe'):
            with self.subTest(extra=extra):
                self.assertTrue(self._errors_for(extra))


class TestConfigModulesStayDecoupled(unittest.TestCase):
    def _modules_loaded_by(self, module):
        # A fresh interpreter, because this test run has already imported everything
        code = f"import sys, {module}; print(' '.join(sys.modules))"
        result = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True, check=True, cwd=REPO_ROOT)
        return result.stdout.split()

    def test_feature_flags_does_not_load_storage(self):
        self.assertNotIn('utils.storage', self._modules_loaded_by('utils.feature_flags'))

    def test_recovery_service_loads_neither_feature_flags_nor_storage(self):
        loaded = self._modules_loaded_by('utils.content_recovery.recovery_service')
        self.assertNotIn('utils.feature_flags', loaded)
        self.assertNotIn('utils.storage', loaded)


if __name__ == '__main__':
    unittest.main()
