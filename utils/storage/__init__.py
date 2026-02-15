"""
Storage provider abstraction layer for Reddit Stash.

Supports multiple cloud storage backends (Dropbox, S3) through a unified
Protocol-based interface with frozen dataclass value types.
"""

from utils.storage.base import (
    StorageProvider,
    StorageFileInfo,
    SyncResult,
    StorageProviderProtocol,
)

__all__ = [
    "StorageProvider",
    "StorageFileInfo",
    "SyncResult",
    "StorageProviderProtocol",
]
