"""
Wayback Machine provider for content recovery.

This module implements content recovery using the Internet Archive's
Wayback Machine APIs. The Wayback Machine is the most reliable and
ethical source for archived web content.

APIs Used:
- Availability API: Check if URL is archived
- CDX Server API: Get detailed capture information
- Memento API: Protocol-compliant access
"""

import time
import requests
import logging
from typing import Any
from urllib.parse import quote, urlparse

from utils.content_recovery.providers.archive_session import archive_session
from utils.content_recovery.recovery_metadata import RecoveryResult, RecoveryMetadata, RecoverySource, RecoveryQuality
from utils.rate_limiter import RateLimitTimeout, parse_retry_after, rate_limit_manager, rate_limited


WAYBACK_AVAILABILITY_API = 'https://archive.org/wayback/available'
WAYBACK_BASE_URL = 'https://web.archive.org'


class WaybackMachineProvider:
    """Provider for recovering content from the Internet Archive's Wayback Machine."""

    def __init__(self, timeout: int = 10):
        self.timeout = timeout
        self.session = archive_session()
        self._logger = logging.getLogger(f"{__name__}.{self.__class__.__name__}")

    def attempt_recovery(self, url: str) -> RecoveryResult:
        """Find the most recent Wayback Machine capture of a URL; every failure is a result."""
        if not rate_limit_manager.acquire('wayback_machine', timeout=30):
            return RecoveryResult.failure_result(
                'wayback_machine recovery deferred by rate limiter',
                RecoverySource.WAYBACK_MACHINE,
            )

        start_time = time.time()

        try:
            self._logger.debug(f"Attempting Wayback Machine recovery for: {url}")

            # Either API can answer alone, but a miss only counts when neither one failed.
            lookup_error = None
            for lookup in (self._lookup_availability, self._search_cdx):
                try:
                    archive_data = lookup(url)
                except (requests.exceptions.RequestException, ValueError, RateLimitTimeout) as e:
                    self._logger.debug(f"Wayback Machine lookup failed: {e}")
                    # After a 429 the CDX token times out too; the 429 is the error worth reporting.
                    lookup_error = lookup_error or e
                    continue
                if archive_data:
                    return self._create_success_result(url, archive_data, time.time() - start_time)

            if lookup_error:
                return RecoveryResult.failure_from_exception(
                    lookup_error, RecoverySource.WAYBACK_MACHINE, start_time
                )

            duration = time.time() - start_time
            return RecoveryResult.failure_result(
                f"No archived version found in Wayback Machine (search duration: {duration:.2f}s)",
                RecoverySource.WAYBACK_MACHINE,
                confirmed_miss=True,
            )

        except Exception as e:
            self._logger.error(f"Unexpected error in Wayback Machine recovery: {e}")
            return RecoveryResult.failure_from_exception(e, RecoverySource.WAYBACK_MACHINE, start_time)

    def _lookup_availability(self, url: str) -> dict[str, Any] | None:
        """Return the closest capture from the Availability API, or None when it has none."""
        encoded_url = quote(url, safe=':/?#[]@!$&\'()*+,;=')
        response = self.session.get(f"{WAYBACK_AVAILABILITY_API}?url={encoded_url}", timeout=self.timeout)
        rate_limit_manager.report_response('wayback_machine', response.status_code, parse_retry_after(response))
        response.raise_for_status()

        payload = response.json()
        snapshots = payload.get('archived_snapshots') if isinstance(payload, dict) else None
        if not isinstance(snapshots, dict):
            raise ValueError('unexpected Availability API response shape')

        closest = snapshots.get('closest', {})
        if not closest.get('available'):
            return None

        self._logger.debug(f"Found archived version via Availability API: {closest.get('url')}")
        return {
            'url': closest.get('url'),
            'timestamp': closest.get('timestamp'),
            'status': closest.get('status'),
            'api_source': 'availability'
        }

    # attempt_recovery's token covers the Availability request; this one is a second request.
    @rate_limited('wayback_machine', timeout=30)
    def _search_cdx(self, url: str) -> dict[str, Any] | None:
        """Return the most recent successful capture from the CDX API, or None when it has none."""
        if not urlparse(url).netloc:
            return None

        params = {
            'url': url,
            'matchType': 'exact',
            'output': 'json',
            'limit': 10,
            'filter': 'statuscode:200',
            'sort': 'timestamp',
            'reverse': 'true',
        }
        response = self.session.get(f"{WAYBACK_BASE_URL}/cdx/search/cdx", params=params, timeout=self.timeout)
        rate_limit_manager.report_response('wayback_machine', response.status_code, parse_retry_after(response))
        response.raise_for_status()

        data = response.json()
        if not isinstance(data, list):
            raise ValueError('unexpected CDX API response shape')

        # CDX format: [urlkey, timestamp, original, mimetype, statuscode, digest, length]
        # The first row is headers.
        if len(data) < 2:
            return None

        capture = data[1]
        if not isinstance(capture, list) or len(capture) < 3:
            raise ValueError('unexpected CDX API capture row')

        timestamp = capture[1]
        wayback_url = f"{WAYBACK_BASE_URL}/web/{timestamp}/{capture[2]}"
        self._logger.debug(f"Found archived version via CDX API: {wayback_url}")
        return {
            'url': wayback_url,
            'timestamp': timestamp,
            'status': capture[4] if len(capture) > 4 else 'unknown',
            'mimetype': capture[3] if len(capture) > 3 else 'unknown',
            'api_source': 'cdx'
        }

    def _create_success_result(self, original_url: str, archive_data: dict[str, Any],
                             duration: float) -> RecoveryResult:
        """Create a successful recovery result with metadata."""

        # Determine content quality based on timestamp and API source
        quality = self._assess_content_quality(archive_data)

        metadata = RecoveryMetadata(
            source=RecoverySource.WAYBACK_MACHINE,
            recovered_url=archive_data['url'],
            recovery_timestamp=time.time(),
            content_quality=quality,
            attempt_duration=duration,
            additional_metadata={
                'wayback_timestamp': archive_data.get('timestamp'),
                'status_code': archive_data.get('status'),
                'api_source': archive_data.get('api_source'),
                'mimetype': archive_data.get('mimetype')
            }
        )

        return RecoveryResult.success_result(archive_data['url'], metadata)

    def _assess_content_quality(self, archive_data: dict[str, Any]) -> RecoveryQuality:
        """Assess the quality of archived content based on metadata."""
        timestamp = archive_data.get('timestamp', '')
        status = archive_data.get('status')

        # Parse timestamp to assess age
        try:
            if len(timestamp) >= 8:  # YYYYMMDD format
                year = int(timestamp[:4])
                current_year = time.gmtime().tm_year
                age_years = current_year - year

                # Quality assessment based on age and status
                if status == '200' and age_years <= 1:
                    return RecoveryQuality.HIGH_QUALITY
                elif status == '200' and age_years <= 3:
                    return RecoveryQuality.MEDIUM_QUALITY
                else:
                    return RecoveryQuality.LOW_QUALITY
        except (ValueError, TypeError) as e:
            self._logger.debug(f"Could not parse Wayback timestamp {timestamp!r}: {e}")

        # Default quality for archived content
        return RecoveryQuality.MEDIUM_QUALITY
