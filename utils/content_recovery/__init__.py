"""
Content Recovery System for Reddit Stash.

This module provides sophisticated content recovery capabilities for handling
digital decay and link rot. When original content becomes unavailable, the
recovery system attempts to retrieve it from multiple archival sources.

Key Features:
- Multi-provider recovery cascade (Wayback Machine, PullPush.io, Reddit Previews, Reveddit)
- Intelligent rate limiting and caching
- Comprehensive metadata and provenance tracking
- Seamless integration with existing media download system

Usage:
    from utils.content_recovery import ContentRecoveryService

    recovery_service = ContentRecoveryService()
    recovered_url = recovery_service.attempt_recovery(failed_url)
"""

from .recovery_service import ContentRecoveryService
from .cache_manager import RecoveryCacheManager
from .recovery_metadata import RecoveryResult, RecoveryMetadata, RecoverySource

__all__ = [
    'ContentRecoveryService',
    'RecoveryCacheManager',
    'RecoveryResult',
    'RecoveryMetadata',
    'RecoverySource'
]