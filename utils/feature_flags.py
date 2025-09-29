"""
Feature flags module for Reddit Stash media downloads.

This module provides a clean interface for checking feature flags and loading
media-related configuration with proper fallbacks and validation.
"""

import os
import configparser
from typing import Dict, Any, Optional


class MediaFeatureConfig:
    """Configuration container for media download features."""

    def __init__(self):
        self.config_parser = configparser.ConfigParser()
        self._load_config()

    def _load_config(self):
        """Load configuration from settings.ini."""
        # Dynamically determine the path to the root directory
        BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        config_file_path = os.path.join(BASE_DIR, 'settings.ini')

        # Read the settings.ini file
        self.config_parser.read(config_file_path)

    def is_media_enabled(self) -> bool:
        """Check if media downloads are enabled globally."""
        return self.config_parser.getboolean('Media', 'download_enabled', fallback=False)

    def is_images_enabled(self) -> bool:
        """Check if image downloads are enabled."""
        return (self.is_media_enabled() and
                self.config_parser.getboolean('Media', 'download_images', fallback=True))

    def is_videos_enabled(self) -> bool:
        """Check if video downloads are enabled."""
        return (self.is_media_enabled() and
                self.config_parser.getboolean('Media', 'download_videos', fallback=True))

    def is_audio_enabled(self) -> bool:
        """Check if audio downloads are enabled."""
        return (self.is_media_enabled() and
                self.config_parser.getboolean('Media', 'download_audio', fallback=True))

    def is_albums_enabled(self) -> bool:
        """Check if album downloads are enabled."""
        return (self.is_media_enabled() and
                self.config_parser.getboolean('Media', 'download_albums', fallback=True))

    def is_thumbnails_enabled(self) -> bool:
        """Check if thumbnail generation is enabled."""
        return (self.is_media_enabled() and
                self.config_parser.getboolean('Media', 'create_thumbnails', fallback=True))

    def get_media_config(self) -> Dict[str, Any]:
        """Get all media configuration as a dictionary."""
        if not self.is_media_enabled():
            return {'media_enabled': False}

        return {
            'media_enabled': True,
            'images_enabled': self.is_images_enabled(),
            'videos_enabled': self.is_videos_enabled(),
            'audio_enabled': self.is_audio_enabled(),
            'albums_enabled': self.is_albums_enabled(),
            'thumbnails_enabled': self.is_thumbnails_enabled(),

            # Image settings
            'thumbnail_size': self.config_parser.getint('Media', 'thumbnail_size', fallback=800),
            'max_image_size': self.config_parser.getint('Media', 'max_image_size', fallback=5242880),  # 5MB

            # Video settings
            'video_quality': self.config_parser.get('Media', 'video_quality', fallback='high'),
            'max_video_size': self.config_parser.getint('Media', 'max_video_size', fallback=209715200),  # 200MB

            # Album settings
            'max_album_images': self.config_parser.getint('Media', 'max_album_images', fallback=50),

            # Performance settings
            'max_concurrent_downloads': self.config_parser.getint('Media', 'max_concurrent_downloads', fallback=3),
            'download_timeout': self.config_parser.getint('Media', 'download_timeout', fallback=30),
            'max_daily_storage_mb': self.config_parser.getint('Media', 'max_daily_storage_mb', fallback=1024),
        }

    def get_imgur_config(self) -> Dict[str, Any]:
        """Get Imgur API configuration."""
        config = {
            'recover_deleted': self.config_parser.getboolean('Imgur', 'recover_deleted', fallback=True),
            'client_ids': None,
            'client_secrets': None,
        }

        # Handle client IDs (comma-separated list)
        client_ids_str = self.config_parser.get('Imgur', 'client_ids', fallback='None')
        if client_ids_str and client_ids_str.lower() != 'none':
            config['client_ids'] = [id.strip() for id in client_ids_str.split(',') if id.strip()]

        # Handle client secrets (comma-separated list)
        client_secrets_str = self.config_parser.get('Imgur', 'client_secrets', fallback='None')
        if client_secrets_str and client_secrets_str.lower() != 'none':
            config['client_secrets'] = [secret.strip() for secret in client_secrets_str.split(',') if secret.strip()]

        return config

    def get_recovery_config(self) -> Dict[str, Any]:
        """Get content recovery configuration."""
        return {
            'use_wayback_machine': self.config_parser.getboolean('Recovery', 'use_wayback_machine', fallback=True),
            'use_pushshift_api': self.config_parser.getboolean('Recovery', 'use_pushshift_api', fallback=True),
            'use_reddit_previews': self.config_parser.getboolean('Recovery', 'use_reddit_previews', fallback=True),
            'use_reveddit_api': self.config_parser.getboolean('Recovery', 'use_reveddit_api', fallback=False),
            'timeout_seconds': self.config_parser.getint('Recovery', 'timeout_seconds', fallback=10),
            'cache_duration_hours': self.config_parser.getint('Recovery', 'cache_duration_hours', fallback=24),
        }

    def validate_config(self) -> Optional[str]:
        """
        Validate the media configuration and return error message if invalid.

        Returns:
            None if config is valid, error message string if invalid.
        """
        if not self.is_media_enabled():
            return None  # No validation needed if media is disabled

        config = self.get_media_config()

        # Validate size limits
        if config['max_image_size'] <= 0:
            return "max_image_size must be greater than 0"

        if config['max_video_size'] <= 0:
            return "max_video_size must be greater than 0"

        if config['thumbnail_size'] <= 0:
            return "thumbnail_size must be greater than 0"

        # Validate album limits
        if config['max_album_images'] < 0:
            return "max_album_images must be 0 (unlimited) or positive"

        # Validate performance settings
        if config['max_concurrent_downloads'] <= 0:
            return "max_concurrent_downloads must be greater than 0"

        if config['download_timeout'] <= 0:
            return "download_timeout must be greater than 0"

        if config['max_daily_storage_mb'] <= 0:
            return "max_daily_storage_mb must be greater than 0"

        # Validate video quality
        if config['video_quality'] not in ['high', 'low']:
            return "video_quality must be 'high' or 'low'"

        recovery_config = self.get_recovery_config()
        if recovery_config['timeout_seconds'] <= 0:
            return "Recovery timeout_seconds must be greater than 0"

        if recovery_config['cache_duration_hours'] <= 0:
            return "Recovery cache_duration_hours must be greater than 0"

        return None


# Global instance for easy access
_media_config = None

def get_media_config() -> MediaFeatureConfig:
    """Get the global media configuration instance."""
    global _media_config
    if _media_config is None:
        _media_config = MediaFeatureConfig()
    return _media_config

def is_media_enabled() -> bool:
    """Quick check if media downloads are enabled globally."""
    return get_media_config().is_media_enabled()

def validate_media_config() -> Optional[str]:
    """Validate media configuration and return error if invalid."""
    return get_media_config().validate_config()

def get_feature_summary() -> str:
    """Get a summary of enabled features for logging/debugging."""
    config = get_media_config()

    if not config.is_media_enabled():
        return "Media downloads: DISABLED"

    features = []
    if config.is_images_enabled():
        features.append("images")
    if config.is_videos_enabled():
        features.append("videos")
    if config.is_audio_enabled():
        features.append("audio")
    if config.is_albums_enabled():
        features.append("albums")
    if config.is_thumbnails_enabled():
        features.append("thumbnails")

    return f"Media downloads: ENABLED ({', '.join(features)})"