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
from typing import Optional, Dict, Any
from urllib.parse import quote, urlparse

from utils.content_recovery.recovery_metadata import RecoveryResult, RecoveryMetadata, RecoverySource, RecoveryQuality
from utils.rate_limiter import rate_limited


class WaybackMachineProvider:
    """Provider for recovering content from the Internet Archive's Wayback Machine."""

    def __init__(self, timeout: int = 10):
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Reddit Stash Content Recovery/1.0 (Personal Archive Tool)',
            'Accept': 'application/json'
        })
        self._logger = logging.getLogger(f"{__name__}.{self.__class__.__name__}")

        # Wayback Machine API endpoints
        self.availability_api = "http://archive.org/wayback/available"
        self.cdx_api = "http://web.archive.org/cdx/search/cdx"

    @rate_limited('wayback_machine', timeout=30)
    def attempt_recovery(self, url: str, prefer_recent: bool = True) -> RecoveryResult:
        """
        Attempt to recover content from the Wayback Machine.

        Args:
            url: Original URL to recover
            prefer_recent: Whether to prefer more recent snapshots

        Returns:
            RecoveryResult with status and recovered URL if successful
        """
        start_time = time.time()

        try:
            self._logger.debug(f"Attempting Wayback Machine recovery for: {url}")

            # First, try the simple Availability API
            availability_result = self._check_availability(url)
            if availability_result:
                duration = time.time() - start_time
                return self._create_success_result(url, availability_result, duration)

            # If availability API fails, try CDX Server API for more detailed search
            cdx_result = self._search_cdx(url, prefer_recent)
            if cdx_result:
                duration = time.time() - start_time
                return self._create_success_result(url, cdx_result, duration)

            # No archived version found
            duration = time.time() - start_time
            return RecoveryResult.failure_result(
                f"No archived version found in Wayback Machine (search duration: {duration:.2f}s)",
                RecoverySource.WAYBACK_MACHINE
            )

        except requests.exceptions.Timeout:
            duration = time.time() - start_time
            return RecoveryResult.failure_result(
                f"Wayback Machine request timed out after {duration:.2f}s",
                RecoverySource.WAYBACK_MACHINE
            )
        except requests.exceptions.RequestException as e:
            duration = time.time() - start_time
            return RecoveryResult.failure_result(
                f"Wayback Machine request failed: {str(e)} (duration: {duration:.2f}s)",
                RecoverySource.WAYBACK_MACHINE
            )
        except Exception as e:
            duration = time.time() - start_time
            self._logger.error(f"Unexpected error in Wayback Machine recovery: {e}")
            return RecoveryResult.failure_result(
                f"Unexpected error: {str(e)} (duration: {duration:.2f}s)",
                RecoverySource.WAYBACK_MACHINE
            )

    def _check_availability(self, url: str) -> Optional[Dict[str, Any]]:
        """Check URL availability using the Wayback Machine Availability API."""
        try:
            encoded_url = quote(url, safe=':/?#[]@!$&\'()*+,;=')
            availability_url = f"{self.availability_api}?url={encoded_url}"

            response = self.session.get(availability_url, timeout=self.timeout)
            response.raise_for_status()

            data = response.json()
            archived_snapshots = data.get('archived_snapshots', {})
            closest = archived_snapshots.get('closest', {})

            if closest.get('available'):
                self._logger.debug(f"Found archived version via Availability API: {closest.get('url')}")
                return {
                    'url': closest.get('url'),
                    'timestamp': closest.get('timestamp'),
                    'status': closest.get('status'),
                    'api_source': 'availability'
                }

        except (requests.exceptions.RequestException, ValueError) as e:
            self._logger.debug(f"Availability API failed: {e}")

        return None

    def _search_cdx(self, url: str, prefer_recent: bool = True) -> Optional[Dict[str, Any]]:
        """Search for URL using the CDX Server API for more detailed results."""
        try:
            # Parse URL to search for domain and path patterns
            parsed = urlparse(url)
            if not parsed.netloc:
                return None

            # CDX API parameters
            params = {
                'url': url,
                'matchType': 'exact',
                'output': 'json',
                'limit': 10,  # Get up to 10 results
                'filter': 'statuscode:200'  # Only successful captures
            }

            if prefer_recent:
                params['sort'] = 'timestamp'
                params['reverse'] = 'true'  # Most recent first

            response = self.session.get(self.cdx_api, params=params, timeout=self.timeout)
            response.raise_for_status()

            data = response.json()

            # CDX format: [urlkey, timestamp, original, mimetype, statuscode, digest, length]
            if len(data) > 1:  # First row is headers
                # Take the first result (most recent if sorted)
                capture = data[1]
                if len(capture) >= 3:
                    timestamp = capture[1]
                    original_url = capture[2]

                    # Construct wayback URL
                    wayback_url = f"http://web.archive.org/web/{timestamp}/{original_url}"

                    self._logger.debug(f"Found archived version via CDX API: {wayback_url}")
                    return {
                        'url': wayback_url,
                        'timestamp': timestamp,
                        'status': capture[4] if len(capture) > 4 else 'unknown',
                        'mimetype': capture[3] if len(capture) > 3 else 'unknown',
                        'api_source': 'cdx'
                    }

        except (requests.exceptions.RequestException, ValueError, IndexError) as e:
            self._logger.debug(f"CDX API search failed: {e}")

        return None

    def _create_success_result(self, original_url: str, archive_data: Dict[str, Any],
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

    def _assess_content_quality(self, archive_data: Dict[str, Any]) -> RecoveryQuality:
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
                elif status == '200':
                    return RecoveryQuality.LOW_QUALITY
                else:
                    return RecoveryQuality.LOW_QUALITY
        except (ValueError, TypeError):
            pass

        # Default quality for archived content
        return RecoveryQuality.MEDIUM_QUALITY

    def get_provider_info(self) -> Dict[str, Any]:
        """Get information about this recovery provider."""
        return {
            'name': 'Wayback Machine',
            'source': RecoverySource.WAYBACK_MACHINE,
            'description': 'Internet Archive Wayback Machine - free web archive',
            'rate_limits': 'No hard limits, requests to be gentle and respectful',
            'ethics': 'Fully ethical - public archive service designed for this purpose',
            'reliability': 'Very high - operated by Internet Archive since 1996',
            'coverage': 'Broad web coverage with billions of archived pages'
        }