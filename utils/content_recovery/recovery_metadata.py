"""
Recovery metadata and result structures for content recovery system.

This module defines the data structures used to track recovery attempts,
results, and provenance information for archived content.
"""

import time
from enum import Enum
from dataclasses import dataclass
from typing import Any
from datetime import datetime


class ArchiveLookupError(Exception):
    """Some requested IDs got no answer; records holds what the other batches returned."""

    def __init__(self, message: str, records: dict[str, dict[str, Any]]):
        super().__init__(message)
        self.records = records


class RecoverySource(Enum):
    """Enumeration of content recovery sources."""
    WAYBACK_MACHINE = "wayback_machine"
    ARCTIC_SHIFT = "arctic_shift"
    PULLPUSH_IO = "pullpush_io"


class RecoveryQuality(Enum):
    """Quality assessment of recovered content."""
    ORIGINAL = "original"           # Exact original content
    HIGH_QUALITY = "high_quality"   # High-quality archive
    MEDIUM_QUALITY = "medium_quality"  # Acceptable quality
    LOW_QUALITY = "low_quality"     # Poor quality but usable
    METADATA_ONLY = "metadata_only" # Only metadata recovered


@dataclass
class RecoveryMetadata:
    """Metadata about a content recovery attempt."""
    source: RecoverySource
    recovered_url: str | None
    recovery_timestamp: float
    content_quality: RecoveryQuality
    original_failure_reason: str | None = None
    cache_hit: bool = False
    attempt_duration: float | None = None
    additional_metadata: dict[str, Any] | None = None

    @property
    def recovery_date(self) -> str:
        """Human-readable recovery date."""
        return datetime.fromtimestamp(self.recovery_timestamp).strftime('%Y-%m-%d %H:%M:%S')


@dataclass
class RecoveryResult:
    """Result of a content recovery attempt."""
    success: bool
    recovered_url: str | None = None
    metadata: RecoveryMetadata | None = None
    error_message: str | None = None
    source: RecoverySource | None = None
    attempted_sources: frozenset[RecoverySource] = frozenset()
    negative_cache_sources: frozenset[RecoverySource] = frozenset()
    # The archive answered and does not hold the item; any other failure may just be an outage.
    confirmed_miss: bool = False

    @classmethod
    def success_result(
        cls,
        url: str | None,
        metadata: RecoveryMetadata,
        attempted_sources: frozenset[RecoverySource] = frozenset(),
    ) -> 'RecoveryResult':
        """Create a successful recovery result."""
        return cls(
            success=True,
            recovered_url=url,
            metadata=metadata,
            source=metadata.source,
            attempted_sources=attempted_sources | frozenset({metadata.source}),
        )

    @classmethod
    def failure_result(
        cls,
        error: str,
        source: RecoverySource | None = None,
        attempted_sources: frozenset[RecoverySource] = frozenset(),
        negative_cache_sources: frozenset[RecoverySource] = frozenset(),
        confirmed_miss: bool = False,
    ) -> 'RecoveryResult':
        """Create a failed recovery result."""
        return cls(
            success=False,
            error_message=error,
            source=source,
            attempted_sources=(
                attempted_sources | frozenset({source})
                if source is not None else attempted_sources
            ),
            negative_cache_sources=negative_cache_sources,
            confirmed_miss=confirmed_miss,
        )

    @classmethod
    def failure_from_exception(
        cls,
        error: Exception,
        source: RecoverySource,
        started_at: float,
    ) -> 'RecoveryResult':
        """Create a failure for a lookup that could not get an answer from its archive."""
        duration = time.time() - started_at
        return cls.failure_result(f"{source.value} lookup failed: {error} (duration: {duration:.2f}s)", source)


@dataclass
class RecoveryAttempt:
    """Record of a recovery attempt for database storage."""
    original_url: str
    recovery_source: RecoverySource
    id: int | None = None
    attempted_at: float = 0.0
    success: bool = False
    recovered_url: str | None = None
    error_message: str | None = None
    duration_seconds: float | None = None

    def __post_init__(self):
        if self.attempted_at == 0.0:
            self.attempted_at = time.time()


@dataclass
class RecoveryCacheEntry:
    """Cache entry for recovery results."""
    url_hash: str
    original_url: str
    recovery_source: RecoverySource
    recovered_url: str | None
    content_quality: RecoveryQuality
    cached_at: float
    expires_at: float
    metadata_json: str | None = None
    success: bool = True
    id: int | None = None
