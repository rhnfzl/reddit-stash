"""
Content Recovery System for Reddit Stash.

When a media URL no longer works, tries to fetch it from the Wayback Machine,
Arctic Shift and PullPush.io, caching results and recording which source was used.
The GDPR export also uses the Arctic Shift and PullPush clients to look up archived
post and comment text by ID; both raise ArchiveLookupError on an incomplete lookup.

Usage:
    from utils.content_recovery import ContentRecoveryService

    recovery_service = ContentRecoveryService()
    result = recovery_service.attempt_recovery(failed_url)
"""

from utils.content_recovery.arctic_shift import ArcticShiftClient
from utils.content_recovery.recovery_service import ContentRecoveryService
from utils.content_recovery.cache_manager import RecoveryCacheManager
from utils.content_recovery.recovery_metadata import ArchiveLookupError, RecoveryResult, RecoveryMetadata, RecoveryQuality, RecoverySource

__all__ = [
    'ArchiveLookupError',
    'ArcticShiftClient',
    'ContentRecoveryService',
    'RecoveryCacheManager',
    'RecoveryResult',
    'RecoveryMetadata',
    'RecoveryQuality',
    'RecoverySource'
]
