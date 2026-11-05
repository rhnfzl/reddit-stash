"""
Feature flags module for Reddit Stash media downloads.

This module provides a clean interface for checking feature flags and loading
media-related configuration with proper fallbacks and validation.
"""

from functools import cache

from utils.env_config import load_settings


class MediaFeatureConfig:
    """Configuration container for media download features."""

    def __init__(self):
        self.config_parser = load_settings()

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

    def is_albums_enabled(self) -> bool:
        """Check if album downloads are enabled."""
        return (self.is_media_enabled() and
                self.config_parser.getboolean('Media', 'download_albums', fallback=True))

    def max_concurrent_downloads(self) -> int:
        """Worker count for downloading a gallery's images in parallel."""
        return self.config_parser.getint('Media', 'max_concurrent_downloads', fallback=3)

    def validate_config(self) -> str | None:
        """
        Validate the media configuration and return error message if invalid.

        Returns:
            None if config is valid, error message string if invalid.
        """
        if not self.is_media_enabled():
            return None  # No validation needed if media is disabled

        # A malformed boolean raises here, before any download starts
        self.is_images_enabled()
        self.is_videos_enabled()
        self.is_albums_enabled()

        if self.max_concurrent_downloads() <= 0:
            return "max_concurrent_downloads must be greater than 0"

        return None


@cache
def get_media_config() -> MediaFeatureConfig:
    """Get the shared media configuration instance."""
    return MediaFeatureConfig()

def validate_media_config() -> str | None:
    """Validate media configuration and return error if invalid."""
    return get_media_config().validate_config()


def get_feature_summary() -> str:
    """Get a summary of enabled media features for logging/debugging."""
    config = get_media_config()

    if not config.is_media_enabled():
        return "Media downloads: DISABLED"

    features = []
    if config.is_images_enabled():
        features.append("images")
    if config.is_videos_enabled():
        features.append("videos")
    if config.is_albums_enabled():
        features.append("albums")
    return f"Media downloads: ENABLED ({', '.join(features)})"
