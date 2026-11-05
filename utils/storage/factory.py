"""
Storage provider factory and configuration loading.

Reads the [Storage] section from settings.ini (env vars override)
and returns the appropriate provider instance.
"""

import configparser
import os
import warnings
from dataclasses import dataclass

from utils.env_config import load_settings
from utils.path_security import ensure_writable_directory
from utils.storage.base import StorageProvider, StorageProviderProtocol

_INVALID = (None, "", "None")


def configured_local_dir() -> str:
    """The local save directory from settings.ini, as an absolute path."""
    return os.path.abspath(load_settings().get("Settings", "save_directory", fallback="reddit/"))


def load_local_dir() -> str:
    """The local save directory, created if needed; raises OSError when it cannot be written."""
    return ensure_writable_directory(configured_local_dir())


def load_sync_check_type() -> str:
    """The check_type setting as the storage CLI reads it."""
    # DIR when unset, unlike the backup's LOG: LOG here would change what a sync transfers.
    return load_settings().get("Settings", "check_type", fallback="DIR").upper()


@dataclass
class StorageConfig:
    """All storage-related configuration."""
    provider: StorageProvider = StorageProvider.NONE
    # Dropbox folder or S3 key prefix, read from the dropbox_directory setting
    remote_directory: str = "/reddit"
    # S3
    s3_bucket: str | None = None
    s3_region: str | None = None
    s3_storage_class: str = "STANDARD_IA"
    s3_endpoint_url: str | None = None


def load_storage_config(parser: configparser.ConfigParser | None = None) -> StorageConfig:
    """Load storage configuration from settings.ini (or an already parsed copy) with env var overrides."""
    if parser is None:
        parser = load_settings()

    def _get(section: str, key: str, fallback: str | None = None) -> str | None:
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

    # [Settings] dropbox_directory configures the remote root for every provider
    remote_dir = _get("Settings", "dropbox_directory", "/reddit")

    # S3 settings — env vars override
    s3_bucket = os.getenv("AWS_S3_BUCKET") or _get("Storage", "s3_bucket")
    s3_region = os.getenv("AWS_DEFAULT_REGION") or _get("Storage", "s3_region")
    s3_storage_class = os.getenv("S3_STORAGE_CLASS") or _get("Storage", "s3_storage_class", "STANDARD_IA")
    s3_endpoint_url = os.getenv("S3_ENDPOINT_URL") or _get("Storage", "s3_endpoint_url")

    return StorageConfig(
        provider=provider,
        remote_directory=remote_dir,
        s3_bucket=s3_bucket,
        s3_region=s3_region,
        s3_storage_class=s3_storage_class,
        s3_endpoint_url=s3_endpoint_url,
    )


def get_storage_summary() -> str:
    """Get a summary of cloud storage configuration."""
    try:
        config = load_storage_config()

        if config.provider == StorageProvider.NONE:
            return "Cloud storage: DISABLED"

        if config.provider == StorageProvider.DROPBOX:
            return f"Cloud storage: Dropbox ({config.remote_directory})"

        if config.provider == StorageProvider.S3:
            parts = [f"Cloud storage: S3 (s3://{config.s3_bucket}"]
            parts.append(f", class={config.s3_storage_class}")
            if config.s3_endpoint_url:
                parts.append(f", endpoint={config.s3_endpoint_url}")
            parts.append(")")
            return "".join(parts)

        return f"Cloud storage: {config.provider.value}"
    except Exception:
        return "Cloud storage: NOT CONFIGURED"


def get_remote_directory(provider: StorageProvider, config: StorageConfig) -> str:
    """Remote root for a provider: the Dropbox folder, or the same path as an S3 key prefix."""
    if provider == StorageProvider.S3:
        return config.remote_directory.strip("/")
    return config.remote_directory


def get_storage_provider(config: StorageConfig | None = None) -> StorageProviderProtocol | None:
    """
    Factory: return the configured storage provider instance (not yet connected).

    Returns None when provider is NONE. The explicit provider setting decides;
    a warning notes when Dropbox credentials are also present under S3.
    """
    if config is None:
        config = load_storage_config()

    if config.provider == StorageProvider.S3 and config.s3_bucket and os.getenv("DROPBOX_REFRESH_TOKEN"):
        warnings.warn(
            "Both Dropbox and S3 credentials detected. Using S3 as configured.",
            stacklevel=2,
        )

    return create_provider(config.provider, config)


def create_provider(provider: StorageProvider, config: StorageConfig) -> StorageProviderProtocol | None:
    """Build the given provider from config (not yet connected); None for NONE."""
    if provider == StorageProvider.DROPBOX:
        try:
            from utils.storage.dropbox_provider import DropboxStorageProvider
        except ModuleNotFoundError as exc:
            if exc.name != "dropbox":
                raise
            raise ValueError(
                "Dropbox provider selected but the dropbox package is not installed. "
                "Run: pip install -r requirements-dropbox.txt"
            ) from exc
        return DropboxStorageProvider()

    if provider == StorageProvider.S3:
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
