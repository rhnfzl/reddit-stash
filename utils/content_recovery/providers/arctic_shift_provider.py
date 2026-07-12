"""Content recovery provider backed by Arctic Shift archived Reddit data."""

import logging
import re
import time
from typing import Optional, Tuple
from urllib.parse import urlparse

from ..arctic_shift import ArcticShiftClient
from ..recovery_metadata import RecoveryMetadata, RecoveryQuality, RecoveryResult, RecoverySource


class ArcticShiftProvider:
    """Recover Reddit post or comment text as metadata rather than media."""

    def __init__(self, timeout: int = 10, client: Optional[ArcticShiftClient] = None):
        self.timeout = timeout
        self.client = client or ArcticShiftClient(timeout=timeout)
        self._logger = logging.getLogger(f'{__name__}.{self.__class__.__name__}')

    def attempt_recovery(self, url: str) -> RecoveryResult:
        """Retrieve archived text for a Reddit permalink when available."""
        started_at = time.time()
        parsed_item = self._parse_reddit_permalink(url)
        if not parsed_item:
            return RecoveryResult.failure_result(
                'URL is not a supported Reddit permalink',
                RecoverySource.ARCTIC_SHIFT,
            )

        content_type, item_id = parsed_item
        try:
            records = (
                self.client.fetch_posts([item_id])
                if content_type == 'post'
                else self.client.fetch_comments([item_id])
            )
        except Exception as error:
            self._logger.warning(f'Arctic Shift recovery failed: {error}')
            return RecoveryResult.failure_result(str(error), RecoverySource.ARCTIC_SHIFT)

        record = records.get(item_id)
        if not record:
            return RecoveryResult.failure_result(
                'Content not found in Arctic Shift archive',
                RecoverySource.ARCTIC_SHIFT,
            )

        metadata = RecoveryMetadata(
            source=RecoverySource.ARCTIC_SHIFT,
            recovered_url=None,
            recovery_timestamp=time.time(),
            content_quality=RecoveryQuality.METADATA_ONLY,
            attempt_duration=time.time() - started_at,
            additional_metadata={
                **record,
                'content_type': content_type,
                'content_id': item_id,
                'archived_data_available': True,
            },
        )
        return RecoveryResult.success_result(None, metadata)

    @staticmethod
    def _parse_reddit_permalink(url: str) -> Optional[Tuple[str, str]]:
        parsed = urlparse(url)
        host = parsed.hostname.lower() if parsed.hostname else ''
        path_parts = [part for part in parsed.path.split('/') if part]

        if host == 'redd.it' or host.endswith('.redd.it'):
            if path_parts and re.fullmatch(r'[a-z0-9]+', path_parts[0], re.IGNORECASE):
                return 'post', path_parts[0]
            return None

        if host != 'reddit.com' and not host.endswith('.reddit.com'):
            return None

        try:
            comments_index = path_parts.index('comments')
            post_id = path_parts[comments_index + 1]
        except (ValueError, IndexError):
            return None

        if not re.fullmatch(r'[a-z0-9]+', post_id, re.IGNORECASE):
            return None

        comment_index = comments_index + 3
        if len(path_parts) > comment_index:
            comment_id = path_parts[comment_index]
            if re.fullmatch(r'[a-z0-9]+', comment_id, re.IGNORECASE):
                return 'comment', comment_id

        return 'post', post_id

    def get_provider_info(self):
        """Return provider metadata for diagnostics."""
        return {
            'name': 'Arctic Shift',
            'source': RecoverySource.ARCTIC_SHIFT.value,
            'type': 'metadata_only',
        }
