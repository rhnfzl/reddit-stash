"""
Configuration validator for Reddit Stash.

Validates the configuration settings before a run, with error messages and
suggestions for fixing issues.
"""

import os
import configparser
from typing import Any
from utils import env_config
from utils.content_recovery.recovery_config import load_recovery_config
from utils.feature_flags import get_media_config, validate_media_config
from utils.storage.base import StorageProvider
from utils.storage.factory import load_storage_config
from utils.storage.s3_provider import VALID_STORAGE_CLASSES


class ConfigValidationError(Exception):
    """Custom exception for configuration validation errors."""

    def __init__(self, message: str, suggestions: list[str] | None = None):
        self.message = message
        self.suggestions = suggestions or []
        super().__init__(self.message)

    def __str__(self) -> str:
        result = f"Configuration Error: {self.message}"
        if self.suggestions:
            result += "\n\nSuggestions:"
            for suggestion in self.suggestions:
                result += f"\n  • {suggestion}"
        return result


class ConfigValidator:
    """Validates settings.ini, warning about values that fall back to the environment."""

    def __init__(self):
        self.errors = []
        self.warnings = []
        self.config_parser = self._load_config()

    def _load_config(self) -> configparser.ConfigParser:
        """Load configuration from settings.ini."""
        if not os.path.exists(env_config.SETTINGS_PATH):
            raise ConfigValidationError(
                "settings.ini file not found",
                ["Create a settings.ini file in the root directory",
                 "Copy from settings.ini.example if available"]
            )

        try:
            return env_config.load_settings()
        except configparser.Error as e:
            raise ConfigValidationError(
                f"Failed to parse settings.ini: {e}",
                ["Check for syntax errors in settings.ini",
                 "Ensure proper section headers like [Settings]",
                 "Verify key=value format"]
            )

    def validate_required_sections(self) -> None:
        """Validate that required configuration sections exist."""
        required_sections = ['Settings', 'Configuration']
        missing_sections = []

        for section in required_sections:
            if not self.config_parser.has_section(section):
                missing_sections.append(section)

        if missing_sections:
            self.errors.append(
                f"Missing required sections: {', '.join(missing_sections)}"
            )

    def validate_settings_section(self) -> None:
        """Validate the [Settings] section."""
        if not self.config_parser.has_section('Settings'):
            return

        save_dir = self.config_parser.get('Settings', 'save_directory', fallback='reddit/')
        if not save_dir or save_dir.isspace():
            self.errors.append("save_directory cannot be empty")

        save_type = self.config_parser.get('Settings', 'save_type', fallback='ALL').upper()
        valid_save_types = ['ALL', 'SAVED', 'ACTIVITY', 'UPVOTED']
        if save_type not in valid_save_types:
            self.errors.append(
                f"Invalid save_type '{save_type}'. Must be one of: {', '.join(valid_save_types)}"
            )

        check_type = self.config_parser.get('Settings', 'check_type', fallback='LOG').upper()
        valid_check_types = ['LOG', 'DIR']
        if check_type not in valid_check_types:
            self.errors.append(
                f"Invalid check_type '{check_type}'. Must be one of: {', '.join(valid_check_types)}"
            )

        boolean_settings = [
            'unsave_after_download',
            'process_gdpr',
            'process_api',
        ]

        for setting in boolean_settings:
            try:
                self.config_parser.getboolean('Settings', setting, fallback=False)
            except ValueError:
                self.errors.append(
                    f"Invalid boolean value for {setting}. Must be true or false"
                )

    def validate_media_configuration(self) -> None:
        """Validate media download configuration."""
        try:
            media_error = validate_media_config()
            if media_error:
                self.errors.append(f"Media configuration error: {media_error}")
            elif get_media_config().is_media_enabled():
                self._validate_recovery_limits()
        except Exception as e:
            self.errors.append(f"Failed to validate media configuration: {e}")

    def _validate_recovery_limits(self) -> None:
        recovery = load_recovery_config()
        if recovery.timeout_seconds <= 0:
            self.errors.append("Media configuration error: Recovery timeout_seconds must be greater than 0")
        if recovery.cache_duration_hours <= 0:
            self.errors.append("Media configuration error: Recovery cache_duration_hours must be greater than 0")

    def validate_storage_section(self) -> None:
        """Validate the storage settings the run will use, whether from settings.ini or the environment."""
        # The same reader the run uses, so env overrides count here too
        try:
            config = load_storage_config(self.config_parser)
        except ValueError as e:
            self.errors.append(str(e))
            return
        except configparser.InterpolationError as e:
            # A provider=none run never uses these values, so this must not stop it
            self.warnings.append(f"Storage settings could not be read: {e}")
            return
        provider = config.provider

        if provider == StorageProvider.S3:
            if not config.s3_bucket:
                self.errors.append(
                    "S3 provider selected but s3_bucket is not set. "
                    "Set s3_bucket in [Storage] section or AWS_S3_BUCKET env var."
                )

            storage_class = config.s3_storage_class.upper()
            if storage_class not in VALID_STORAGE_CLASSES:
                self.errors.append(
                    f"Invalid s3_storage_class '{storage_class}'. "
                    f"Must be one of: {', '.join(sorted(VALID_STORAGE_CLASSES))}"
                )

            try:
                __import__('boto3')
            except ImportError:
                self.errors.append(
                    "S3 provider selected but boto3 is not installed. "
                    "Install with: pip install -r requirements-s3.txt"
                )

        # Dropbox provider needs credentials
        if provider == StorageProvider.DROPBOX:
            has_creds = all([
                os.getenv('DROPBOX_REFRESH_TOKEN'),
                os.getenv('DROPBOX_APP_KEY'),
                os.getenv('DROPBOX_APP_SECRET'),
            ])
            if not has_creds:
                self.warnings.append(
                    "Dropbox provider selected but DROPBOX_REFRESH_TOKEN, "
                    "DROPBOX_APP_KEY, or DROPBOX_APP_SECRET env vars not set."
                )

    def validate_directory_permissions(self) -> None:
        """Validate that required directories are writable."""
        save_dir = self.config_parser.get('Settings', 'save_directory', fallback='reddit/')

        save_dir = os.path.expanduser(save_dir)

        parent_dir = os.path.dirname(os.path.abspath(save_dir)) if not os.path.isabs(save_dir) else os.path.dirname(save_dir)

        if parent_dir and not os.path.exists(parent_dir):
            self.warnings.append(f"Parent directory for save_directory does not exist: {parent_dir}")
        elif parent_dir and not os.access(parent_dir, os.W_OK):
            self.errors.append(f"No write permission for save_directory parent: {parent_dir}")

    def validate_all(self) -> dict[str, Any]:
        """
        Validate all configuration.

        Returns:
            Dict with validation results including errors, warnings, and summary.
        """
        self.errors = []
        self.warnings = []

        self.validate_required_sections()
        self.validate_settings_section()
        self.validate_media_configuration()
        self.validate_storage_section()
        self.validate_directory_permissions()

        return {
            'valid': len(self.errors) == 0,
            'errors': self.errors,
            'warnings': self.warnings,
            'error_count': len(self.errors),
            'warning_count': len(self.warnings)
        }


def validate_configuration() -> dict[str, Any]:
    """Validate all configuration, raising ConfigValidationError on critical errors."""
    validator = ConfigValidator()
    result = validator.validate_all()

    if not result['valid']:
        error_msg = f"Found {result['error_count']} configuration error(s):\n"
        error_msg += "\n".join(f"  • {error}" for error in result['errors'])

        suggestions = [
            "Check your settings.ini file for syntax errors",
            "Verify all required sections and settings are present",
            "Ensure boolean values are 'true' or 'false'",
            "Check file and directory permissions"
        ]

        raise ConfigValidationError(error_msg, suggestions)

    return result
