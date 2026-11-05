"""settings.ini has one loader, and the entry points use it from any directory."""

import os
import unittest
from contextlib import redirect_stdout
from io import StringIO
from types import SimpleNamespace
from unittest.mock import Mock, patch

import reddit_stash
import storage_utils
from tests.helpers import SettingsFileTestCase
from utils.config_validator import ConfigValidator
from utils.env_config import MissingCredentialsError, load_reddit_credentials
from utils.feature_flags import MediaFeatureConfig
from utils.file_operations import RunStats
from utils.media_services.retry_queue import load_retry_config
from utils.content_recovery.recovery_config import load_cache_config
from utils.storage.base import StorageProvider, SyncResult
from utils.storage.cycle import _coordination_problems
from utils.storage.factory import load_local_dir, load_storage_config, load_sync_check_type


class TestSharedLoader(SettingsFileTestCase):
    SETTINGS = (
        '[Settings]\ncheck_type = LOG\ndropbox_directory = /elsewhere\n'
        '[Media]\ndownload_enabled = true\n'
        '[Retry]\nmax_retries = 42\n'
        '[Recovery]\nmax_cache_entries = 43\n'
    )

    def test_every_reader_sees_the_same_file(self):
        self.assertEqual(load_sync_check_type(), 'LOG')
        self.assertEqual(load_storage_config().remote_directory, '/elsewhere')
        self.assertTrue(MediaFeatureConfig().is_media_enabled())
        self.assertEqual(load_retry_config().max_retries, 42)
        self.assertEqual(load_cache_config().max_cache_entries, 43)

    def test_cache_cleanup_keys_are_read_from_settings(self):
        self.write_settings(
            '[Recovery]\nmax_cache_size_mb = 9\ncleanup_interval_minutes = 7\nenable_background_cleanup = false\n'
        )
        config = load_cache_config()
        self.assertEqual(config.max_cache_size_mb, 9)
        self.assertEqual(config.cleanup_interval_minutes, 7)
        self.assertFalse(config.enable_background_cleanup)

    def test_storage_cli_check_type_default_is_unchanged(self):
        self.write_settings('[Settings]\n')
        self.assertEqual(load_sync_check_type(), 'DIR')

    def test_validator_reports_missing_file(self):
        os.remove(self.settings)
        with self.assertRaisesRegex(Exception, 'settings.ini file not found'):
            ConfigValidator()


class TestStorageCliUsesSharedLoader(SettingsFileTestCase):
    def test_save_directory_is_found_from_another_directory(self):
        wanted = os.path.join(self.root, 'backup')
        self.write_settings(f'[Settings]\nsave_directory = {wanted}\n')
        self.assertEqual(load_local_dir(), wanted)

    def test_upload_reads_a_read_only_save_directory_instead_of_another_folder(self):
        wanted = os.path.join(self.root, 'backup')
        os.mkdir(wanted)
        self.write_settings(f'[Settings]\nsave_directory = {wanted}\n')
        provider = Mock()
        provider.upload_directory.return_value = SyncResult()
        with patch.object(storage_utils, 'load_storage_config', return_value=SimpleNamespace(provider=StorageProvider.S3)), \
                patch.object(storage_utils, '_connected_provider', return_value=provider), \
                patch.object(storage_utils, 'get_remote_directory', return_value='stash'), \
                patch('utils.path_security.os.access', return_value=False), \
                redirect_stdout(StringIO()):
            storage_utils.cmd_upload(None)
        provider.upload_directory.assert_called_once_with(wanted, 'stash')

    def test_coordination_preflight_is_found_from_another_directory(self):
        self.write_settings(
            '[Settings]\nunsave_after_download = true\nprocess_gdpr = true\ncheck_type = LOG\n'
        )
        config = SimpleNamespace(provider=StorageProvider.S3, s3_endpoint_url=None, remote_directory='/stash')
        problems = _coordination_problems(config)
        self.assertEqual(len(problems), 2)
        self.assertIn('unsave_after_download', problems[0])
        self.assertIn('process_gdpr', problems[1])


class TestValidatorModeCase(SettingsFileTestCase):
    SETTINGS = '[Settings]\nsave_type = saved\ncheck_type = log\n'

    def test_lowercase_modes_pass_because_the_save_upper_cases_them(self):
        validator = ConfigValidator()
        validator.validate_settings_section()
        self.assertEqual(validator.errors, [])


class TestCredentialSources(SettingsFileTestCase):
    ENV = {
        'REDDIT_CLIENT_ID': 'env-id', 'REDDIT_CLIENT_SECRET': 'env-secret',
        'REDDIT_USERNAME': 'env-user', 'REDDIT_PASSWORD': 'env-pass',
    }

    def test_settings_value_wins_and_env_fills_the_rest(self):
        self.write_settings('[Configuration]\nclient_id = ini-id\nclient_secret = None\nusername =\n')
        with patch.dict(os.environ, self.ENV):
            self.assertEqual(load_reddit_credentials(), ('ini-id', 'env-secret', 'env-user', 'env-pass'))

    def test_missing_credentials_are_named(self):
        with patch.dict(os.environ, {k: '' for k in self.ENV}):
            with self.assertRaisesRegex(MissingCredentialsError, 'REDDIT_PASSWORD'):
                load_reddit_credentials()


class TestMainUsesSharedLoader(SettingsFileTestCase):
    def _run_main(self):
        output = StringIO()
        with patch('reddit_stash.validate_configuration', return_value={'warnings': []}), \
                patch('reddit_stash.get_feature_summary', return_value=''), \
                redirect_stdout(output):
            reddit_stash.main()
        return output.getvalue()

    def test_settings_are_found_from_another_directory(self):
        self.write_settings(f'[Settings]\nsave_directory = {self.root}\nprocess_api = false\nprocess_gdpr = false\n')
        with patch('reddit_stash.create_reddit_client') as client:
            output = self._run_main()
        client.assert_not_called()
        self.assertIn('Nothing to do', output)

    def _gdpr_only(self):
        self.write_settings(f'[Settings]\nsave_directory = {self.root}\nprocess_api = false\nprocess_gdpr = true\n')

    def test_gdpr_only_run_falls_back_when_credentials_are_missing(self):
        self._gdpr_only()
        with patch('reddit_stash.create_reddit_client', side_effect=MissingCredentialsError('none')), \
                patch('reddit_stash.process_gdpr_export', return_value=RunStats()) as export, \
                patch('reddit_stash.retry_pending_media'):
            output = self._run_main()
        self.assertIn('CSV-only', output)
        self.assertIsNone(export.call_args.args[0])

    def test_truncated_listing_is_reported_in_the_run_summary(self):
        self.write_settings(f'[Settings]\nsave_directory = {self.root}\nprocess_api = true\nprocess_gdpr = false\n')
        with patch('reddit_stash.create_reddit_client'), \
                patch('reddit_stash.save_user_activity', return_value=RunStats(truncated_listings=('saved',))), \
                patch('reddit_stash.retry_pending_media'):
            output = self._run_main()
        self.assertIn('not checked this run: saved', output)

    def test_gdpr_only_run_surfaces_other_client_errors(self):
        self._gdpr_only()
        with patch('reddit_stash.create_reddit_client', side_effect=RuntimeError('broken')), \
                patch('reddit_stash.process_gdpr_export') as export:
            with self.assertRaisesRegex(RuntimeError, 'broken'):
                self._run_main()
        export.assert_not_called()


if __name__ == '__main__':
    unittest.main()
