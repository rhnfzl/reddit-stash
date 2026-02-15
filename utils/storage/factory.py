"""
Storage provider factory and configuration loading.

Reads the [Storage] section from settings.ini (env vars override)
and returns the appropriate provider instance.
"""

import os
import configparser
import warnings
from dataclasses import dataclass
from typing import Optional

from utils.storage.base import StorageProvider

_INVALID = (None, "", "None")


@dataclass
class StorageConfig:
    """All storage-related configuration."""
    provider: StorageProvider = StorageProvider.NONE
    # Dropbox
    dropbox_directory: str = "/reddit"
    # S3
    s3_bucket: Optional[str] = None
    s3_region: Optional[str] = None
    s3_storage_class: str = "STANDARD_IA"
    s3_endpoint_url: Optional[str] = None


def load_storage_config() -> StorageConfig:
    """Load storage configuration from settings.ini with env var overrides."""
    parser = configparser.ConfigParser()
    base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    parser.read(os.path.join(base_dir, "settings.ini"))

    def _get(section: str, key: str, fallback: Optional[str] = None) -> Optional[str]:
        val = parser.get(section, key, fallback=fallback)
        return val if val not in _INVALID else fallback

    # Provider — env var > settings.ini
    provider_str = os.getenv("STORAGE_PROVIDER") or _get("Storage", "provider", "none")
    try:
        provider = StorageProvider(provider_str.lower())
    except ValueError:
        raise ValueError(
            f"Invalid storage provider '{provider_str}'. "
            f"Must be one of: {', '.join(p.value for p in StorageProvider)}"
        )

    # Dropbox directory from [Settings] (existing location)
    dropbox_dir = _get("Settings", "dropbox_directory", "/reddit")

    # S3 settings — env vars override
    s3_bucket = os.getenv("AWS_S3_BUCKET") or _get("Storage", "s3_bucket")
    s3_region = os.getenv("AWS_DEFAULT_REGION") or _get("Storage", "s3_region")
    s3_storage_class = os.getenv("S3_STORAGE_CLASS") or _get("Storage", "s3_storage_class", "STANDARD_IA")
    s3_endpoint_url = os.getenv("S3_ENDPOINT_URL") or _get("Storage", "s3_endpoint_url")

    return StorageConfig(
        provider=provider,
        dropbox_directory=dropbox_dir,
        s3_bucket=s3_bucket,
        s3_region=s3_region,
        s3_storage_class=s3_storage_class,
        s3_endpoint_url=s3_endpoint_url,
    )


def get_storage_provider(config: Optional[StorageConfig] = None):
    """
    Factory: return the configured storage provider instance (not yet connected).

    Returns None when provider is NONE.
    If both Dropbox and S3 are configured, S3 wins with a warning.
    """
    if config is None:
        config = load_storage_config()

    if config.provider == StorageProvider.NONE:
        return None

    # Detect if both have credentials configured
    has_dropbox = bool(os.getenv("DROPBOX_REFRESH_TOKEN"))
    has_s3 = bool(config.s3_bucket)

    if has_dropbox and has_s3 and config.provider != StorageProvider.DROPBOX:
        warnings.warn(
            "Both Dropbox and S3 credentials detected. Using S3 as configured.",
            stacklevel=2,
        )

    if config.provider == StorageProvider.DROPBOX:
        from utils.storage.dropbox_provider import DropboxStorageProvider
        return DropboxStorageProvider(dropbox_directory=config.dropbox_directory)

    if config.provider == StorageProvider.S3:
        if not config.s3_bucket:
            raise ValueError(
                "S3 provider selected but s3_bucket is not set. "
                "Set AWS_S3_BUCKET env var or s3_bucket in [Storage] section."
            )
        from utils.storage.s3_provider import S3StorageProvider
        return S3StorageProvider(
            bucket=config.s3_bucket,
            region=config.s3_region,
            storage_class=config.s3_storage_class,
            endpoint_url=config.s3_endpoint_url,
        )

    return None
