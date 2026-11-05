"""Tests for utils/config_validator.py."""

import os
import unittest
from unittest.mock import patch

from tests.helpers import SettingsFileTestCase
from utils.config_validator import ConfigValidationError, ConfigValidator, validate_configuration
from utils.feature_flags import get_media_config


class TestRetiredIgnoreTlsErrors(unittest.TestCase):
    """Existing settings files may still hold the retired ignore_tls_errors key."""

    def _validate_settings(self, value):
        validator = ConfigValidator()
        validator.config_parser.read_string(f"[Settings]\nignore_tls_errors = {value}\n")
        validator.validate_settings_section()
        return validator

    def test_enabled_key_is_ignored_without_a_security_warning(self):
        validator = self._validate_settings("true")
        self.assertEqual(validator.errors, [])
        self.assertFalse([w for w in validator.warnings if "ignore_tls_errors" in w])

    def test_invalid_value_does_not_fail_validation(self):
        validator = self._validate_settings("maybe")
        self.assertEqual(validator.errors, [])


class TestValidateAll(SettingsFileTestCase):
    """validate_all on small settings files, as the run's startup check calls it."""

    def setUp(self):
        super().setUp()
        get_media_config.cache_clear()
        self.addCleanup(get_media_config.cache_clear)

    def _settings(self, settings='', extra=''):
        self.write_settings(
            f"[Settings]\nsave_directory = {self.root}/saves\n{settings}\n[Configuration]\n{extra}\n"
        )

    def _validate(self):
        return ConfigValidator().validate_all()

    def test_valid_config_has_no_errors(self):
        self._settings('save_type = saved\ncheck_type = dir\nprocess_api = true\n')

        result = self._validate()

        self.assertEqual(result['errors'], [])
        self.assertTrue(result['valid'])
        self.assertEqual(result['error_count'], 0)

    def test_missing_section_is_an_error(self):
        self.write_settings(f'[Settings]\nsave_directory = {self.root}/saves\n')

        result = self._validate()

        self.assertFalse(result['valid'])
        self.assertEqual(result['errors'], ['Missing required sections: Configuration'])

    def test_invalid_choices_are_errors(self):
        for setting, value in (('save_type', 'BOGUS'), ('check_type', 'BOGUS')):
            with self.subTest(setting=setting):
                self._settings(f'{setting} = {value}\n')

                result = self._validate()

                self.assertFalse(result['valid'])
                self.assertIn(f"Invalid {setting} 'BOGUS'", result['errors'][0])

    def test_bad_boolean_is_an_error(self):
        for setting in ('unsave_after_download', 'process_gdpr', 'process_api'):
            with self.subTest(setting=setting):
                self._settings(f'{setting} = maybe\n')

                self.assertEqual(
                    self._validate()['errors'],
                    [f'Invalid boolean value for {setting}. Must be true or false'],
                )

    def test_empty_save_directory_is_an_error(self):
        self.write_settings('[Settings]\nsave_directory =\n[Configuration]\n')

        self.assertIn('save_directory cannot be empty', self._validate()['errors'])

    def test_unwritable_parent_is_an_error_and_a_missing_one_a_warning(self):
        self._settings()
        with patch('utils.config_validator.os.access', return_value=False):
            result = self._validate()
        self.assertEqual(result['errors'], [f'No write permission for save_directory parent: {self.root}'])

        missing = os.path.join(self.root, 'absent')
        self.write_settings(f'[Settings]\nsave_directory = {missing}/saves\n[Configuration]\n')
        result = self._validate()
        self.assertTrue(result['valid'])
        self.assertEqual(result['warning_count'], 1)
        self.assertIn(missing, result['warnings'][0])

    def _media_errors(self, media, extra=''):
        self.write_settings(
            f'[Settings]\nsave_directory = {self.root}/saves\n[Configuration]\n'
            f'[Media]\ndownload_enabled = true\n{media}\n{extra}'
        )
        get_media_config.cache_clear()
        return self._validate()['errors']

    def test_media_rules_apply_when_media_is_enabled(self):
        self.assertEqual(self._media_errors(''), [])
        self.assertIn('max_concurrent_downloads must be greater than 0',
                      self._media_errors('max_concurrent_downloads = 0')[0])
        self.assertIn('Failed to validate media configuration', self._media_errors('download_images = maybe')[0])
        self.assertIn('timeout_seconds must be greater than 0',
                      self._media_errors('', '[Recovery]\ntimeout_seconds = 0\n')[0])

    def test_media_rules_do_not_apply_when_media_is_disabled(self):
        self._settings('', '[Media]\ndownload_enabled = false\nmax_concurrent_downloads = 0\n')

        self.assertEqual(self._validate()['errors'], [])

    def test_validate_configuration_raises_with_every_error(self):
        self._settings('save_type = BOGUS\ncheck_type = BOGUS\n')

        with self.assertRaises(ConfigValidationError) as raised:
            validate_configuration()

        self.assertIn('Found 2 configuration error(s)', raised.exception.message)
        self.assertIn('Suggestions:', str(raised.exception))

    def test_valid_config_returns_the_result(self):
        self._settings()

        self.assertTrue(validate_configuration()['valid'])

    def test_missing_or_broken_file_raises(self):
        os.remove(self.settings)
        with self.assertRaisesRegex(ConfigValidationError, 'not found'):
            ConfigValidator()

        self.write_settings('no section header\n')
        with self.assertRaisesRegex(ConfigValidationError, 'Failed to parse'):
            ConfigValidator()


if __name__ == '__main__':
    unittest.main()
