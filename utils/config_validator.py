"""
Configuration validator for Reddit Stash.

This module provides comprehensive validation for all configuration settings
with helpful error messages and suggestions for fixing issues.
"""

import os
import configparser
from typing import List, Optional, Dict, Any
from .feature_flags import get_media_config, validate_media_config


class ConfigValidationError(Exception):
    """Custom exception for configuration validation errors."""

    def __init__(self, message: str, suggestions: Optional[List[str]] = None):
        self.message = message
        self.suggestions = suggestions or []
        super().__init__(self.message)

    def __str__(self):
        result = f"Configuration Error: {self.message}"
        if self.suggestions:
            result += "\n\nSuggestions:"
            for suggestion in self.suggestions:
                result += f"\n  • {suggestion}"
        return result


class ConfigValidator:
    """Comprehensive configuration validator."""

    def __init__(self):
        self.config_parser = configparser.ConfigParser()
        self.errors = []
        self.warnings = []
        self._load_config()

    def _load_config(self):
        """Load configuration from settings.ini."""
        # Dynamically determine the path to the root directory
        BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        config_file_path = os.path.join(BASE_DIR, 'settings.ini')

        if not os.path.exists(config_file_path):
            raise ConfigValidationError(
                "settings.ini file not found",
                ["Create a settings.ini file in the root directory",
                 "Copy from settings.ini.example if available"]
            )

        try:
            self.config_parser.read(config_file_path)
        except configparser.Error as e:
            raise ConfigValidationError(
                f"Failed to parse settings.ini: {e}",
                ["Check for syntax errors in settings.ini",
                 "Ensure proper section headers like [Settings]",
                 "Verify key=value format"]
            )

    def validate_required_sections(self):
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

    def validate_settings_section(self):
        """Validate the [Settings] section."""
        if not self.config_parser.has_section('Settings'):
            return

        # Validate save_directory
        save_dir = self.config_parser.get('Settings', 'save_directory', fallback='reddit/')
        if not save_dir or save_dir.isspace():
            self.errors.append("save_directory cannot be empty")

        # Validate save_type
        save_type = self.config_parser.get('Settings', 'save_type', fallback='ALL')
        valid_save_types = ['ALL', 'SAVED', 'ACTIVITY', 'UPVOTED']
        if save_type not in valid_save_types:
            self.errors.append(
                f"Invalid save_type '{save_type}'. Must be one of: {', '.join(valid_save_types)}"
            )

        # Validate check_type
        check_type = self.config_parser.get('Settings', 'check_type', fallback='LOG')
        valid_check_types = ['LOG', 'DIR']
        if check_type not in valid_check_types:
            self.errors.append(
                f"Invalid check_type '{check_type}'. Must be one of: {', '.join(valid_check_types)}"
            )

        # Validate boolean settings
        boolean_settings = [
            'unsave_after_download',
            'process_gdpr',
            'process_api',
            'ignore_tls_errors'
        ]

        for setting in boolean_settings:
            try:
                self.config_parser.getboolean('Settings', setting, fallback=False)
            except ValueError:
                self.errors.append(
                    f"Invalid boolean value for {setting}. Must be true or false"
                )

        # Security warning for TLS errors
        if self.config_parser.getboolean('Settings', 'ignore_tls_errors', fallback=False):
            self.warnings.append(
                "ignore_tls_errors is enabled - this reduces security and should only be used for testing"
            )

    def validate_configuration_section(self):
        """Validate the [Configuration] section."""
        if not self.config_parser.has_section('Configuration'):
            return

        # Note: We don't validate Reddit credentials here since env_config.py handles that
        # with proper environment variable fallbacks. Just check format.

        config_keys = ['client_id', 'client_secret', 'username', 'password']
        for key in config_keys:
            value = self.config_parser.get('Configuration', key, fallback=None)
            if value and value.isspace():
                self.warnings.append(f"{key} contains only whitespace - will fallback to environment variable")

    def validate_media_configuration(self):
        """Validate media download configuration."""
        try:
            media_error = validate_media_config()
            if media_error:
                self.errors.append(f"Media configuration error: {media_error}")
        except Exception as e:
            self.errors.append(f"Failed to validate media configuration: {e}")

        # Check for media dependencies if media is enabled
        media_config = get_media_config()
        if media_config.is_media_enabled():
            self._check_media_dependencies()

    def _check_media_dependencies(self):
        """Check if required dependencies are available for media features."""
        optional_imports = {
            'PIL': 'pillow',
            'requests_cache': 'requests-cache',
            'bs4': 'beautifulsoup4',
            'html5lib': 'html5lib'
        }
        # Note: imgurpython removed as it's deprecated and no longer maintained
        # pyimgur is used instead for Imgur API integration

        missing_deps = []
        for module, package in optional_imports.items():
            try:
                __import__(module)
            except ImportError:
                missing_deps.append(package)

        if missing_deps:
            self.warnings.append(
                f"Media downloads enabled but missing optional dependencies: {', '.join(missing_deps)}. "
                f"Install with: pip install {' '.join(missing_deps)}"
            )

    def validate_directory_permissions(self):
        """Validate that required directories are writable."""
        save_dir = self.config_parser.get('Settings', 'save_directory', fallback='reddit/')

        # Expand user home directory if needed
        save_dir = os.path.expanduser(save_dir)

        # Create parent directory path for testing
        parent_dir = os.path.dirname(os.path.abspath(save_dir)) if not os.path.isabs(save_dir) else os.path.dirname(save_dir)

        if parent_dir and not os.path.exists(parent_dir):
            self.warnings.append(f"Parent directory for save_directory does not exist: {parent_dir}")
        elif parent_dir and not os.access(parent_dir, os.W_OK):
            self.errors.append(f"No write permission for save_directory parent: {parent_dir}")

    def validate_all(self) -> Dict[str, Any]:
        """
        Perform comprehensive validation of all configuration.

        Returns:
            Dict with validation results including errors, warnings, and summary.
        """
        self.errors = []
        self.warnings = []

        # Run all validations
        self.validate_required_sections()
        self.validate_settings_section()
        self.validate_configuration_section()
        self.validate_media_configuration()
        self.validate_directory_permissions()

        return {
            'valid': len(self.errors) == 0,
            'errors': self.errors,
            'warnings': self.warnings,
            'error_count': len(self.errors),
            'warning_count': len(self.warnings)
        }

    def get_configuration_summary(self) -> str:
        """Get a human-readable summary of the current configuration."""
        summary = []

        # Basic settings
        save_type = self.config_parser.get('Settings', 'save_type', fallback='ALL')
        process_api = self.config_parser.getboolean('Settings', 'process_api', fallback=True)
        process_gdpr = self.config_parser.getboolean('Settings', 'process_gdpr', fallback=False)

        summary.append(f"Save Type: {save_type}")
        summary.append(f"API Processing: {'Enabled' if process_api else 'Disabled'}")
        summary.append(f"GDPR Processing: {'Enabled' if process_gdpr else 'Disabled'}")

        # Media features summary (from feature_flags module)
        from .feature_flags import get_feature_summary
        summary.append(get_feature_summary())

        return "\n".join(summary)


def validate_configuration() -> Dict[str, Any]:
    """
    Convenience function to validate configuration.

    Returns:
        Dict with validation results.

    Raises:
        ConfigValidationError: If critical configuration errors are found.
    """
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

def print_configuration_summary():
    """Print a summary of the current configuration for debugging."""
    try:
        validator = ConfigValidator()
        print("Configuration Summary:")
        print("=" * 50)
        print(validator.get_configuration_summary())

        result = validator.validate_all()
        if result['warnings']:
            print("\nWarnings:")
            for warning in result['warnings']:
                print(f"  ⚠ {warning}")

        if result['valid']:
            print("\n✅ Configuration is valid")
        else:
            print(f"\n❌ Configuration has {result['error_count']} error(s)")

    except Exception as e:
        print(f"Failed to validate configuration: {e}")