"""
Recovery metadata and result structures for content recovery system.

This module defines the data structures used to track recovery attempts,
results, and provenance information for archived content.
"""

import time
from enum import Enum
from dataclasses import dataclass
from typing import Optional, Dict, Any
from datetime import datetime


class RecoverySource(Enum):
    """Enumeration of content recovery sources."""
    WAYBACK_MACHINE = "wayback_machine"
    PULLPUSH_IO = "pullpush_io"
    REDDIT_PREVIEWS = "reddit_previews"
    REVEDDIT = "reveddit"


class RecoveryQuality(Enum):
    """Quality assessment of recovered content."""
    ORIGINAL = "original"           # Exact original content
    HIGH_QUALITY = "high_quality"   # High-quality archive
    MEDIUM_QUALITY = "medium_quality"  # Acceptable quality
    LOW_QUALITY = "low_quality"     # Poor quality but usable
    THUMBNAIL = "thumbnail"         # Thumbnail or preview only
    METADATA_ONLY = "metadata_only" # Only metadata recovered


@dataclass
class RecoveryMetadata:
    """Metadata about a content recovery attempt."""
    source: RecoverySource
    recovered_url: Optional[str]
    recovery_timestamp: float
    content_quality: RecoveryQuality
    original_failure_reason: Optional[str] = None
    cache_hit: bool = False
    attempt_duration: Optional[float] = None
    additional_metadata: Optional[Dict[str, Any]] = None

    @property
    def recovery_date(self) -> str:
        """Human-readable recovery date."""
        return datetime.fromtimestamp(self.recovery_timestamp).strftime('%Y-%m-%d %H:%M:%S')


@dataclass
class RecoveryResult:
    """Result of a content recovery attempt."""
    success: bool
    recovered_url: Optional[str] = None
    metadata: Optional[RecoveryMetadata] = None
    error_message: Optional[str] = None
    source: Optional[RecoverySource] = None

    @classmethod
    def success_result(cls, url: str, metadata: RecoveryMetadata) -> 'RecoveryResult':
        """Create a successful recovery result."""
        return cls(
            success=True,
            recovered_url=url,
            metadata=metadata,
            source=metadata.source
        )

    @classmethod
    def failure_result(cls, error: str, source: Optional[RecoverySource] = None) -> 'RecoveryResult':
        """Create a failed recovery result."""
        return cls(
            success=False,
            error_message=error,
            source=source
        )


@dataclass
class RecoveryAttempt:
    """Record of a recovery attempt for database storage."""
    id: Optional[int] = None
    original_url: str = ""
    recovery_source: str = ""
    attempted_at: float = 0.0
    success: bool = False
    recovered_url: Optional[str] = None
    error_message: Optional[str] = None
    duration_seconds: Optional[float] = None

    def __post_init__(self):
        if self.attempted_at == 0.0:
            self.attempted_at = time.time()


@dataclass
class RecoveryCacheEntry:
    """Cache entry for recovery results."""
    id: Optional[int] = None
    url_hash: str = ""
    original_url: str = ""
    recovery_source: str = ""
    recovered_url: Optional[str] = None
    content_quality: str = RecoveryQuality.MEDIUM_QUALITY.value
    cached_at: float = 0.0
    expires_at: float = 0.0
    metadata_json: Optional[str] = None

    def __post_init__(self):
        if self.cached_at == 0.0:
            self.cached_at = time.time()

    @property
    def is_expired(self) -> bool:
        """Check if cache entry has expired."""
        return time.time() > self.expires_at