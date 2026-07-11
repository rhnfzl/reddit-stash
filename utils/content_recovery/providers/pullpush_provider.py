"""
PullPush.io provider for content recovery.

This module implements content recovery using PullPush.io, a successor to
Pushshift that archives Reddit data. Note that this service faces legal
challenges from Reddit and has strict rate limits.

Rate Limits (as of 2024):
- Soft limit: 15 requests/minute
- Hard limit: 30 requests/minute
- Long-term limit: 1000 requests/hour

API Documentation: https://pullpush.io/
"""

import time
import requests
import logging
from typing import Optional, Dict, Any, Iterable
from urllib.parse import urlparse
import re

from ..recovery_metadata import RecoveryResult, RecoveryMetadata, RecoverySource, RecoveryQuality
from ...rate_limiter import rate_limited, rate_limit_manager


PULLPUSH_BATCH_SIZE = 100


class PullPushProvider:
    """Provider for recovering Reddit content from PullPush.io API."""

    def __init__(self, timeout: int = 10):
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Reddit Stash Content Recovery/1.0 (Personal Archive Tool)',
            'Accept': 'application/json'
        })
        self._logger = logging.getLogger(f"{__name__}.{self.__class__.__name__}")

        # PullPush.io API endpoints
        self.base_url = "https://api.pullpush.io"
        self.submission_endpoint = f"{self.base_url}/reddit/search/submission"
        self.comment_endpoint = f"{self.base_url}/reddit/search/comment"

        # Rate limiting handled by @rate_limited decorator

    @rate_limited('pullpush_io', timeout=45)
    def attempt_recovery(self, url: str) -> RecoveryResult:
        """
        Attempt to recover Reddit content from PullPush.io.

        Args:
            url: Original Reddit URL to recover

        Returns:
            RecoveryResult with status and recovered URL if successful
        """
        start_time = time.time()

        try:
            self._logger.debug(f"Attempting PullPush.io recovery for: {url}")

            # Parse Reddit URL to extract content ID and type
            reddit_info = self._parse_reddit_url(url)
            if not reddit_info:
                return RecoveryResult.failure_result(
                    "URL is not a valid Reddit URL",
                    RecoverySource.PULLPUSH_IO
                )

            # Search for content based on type
            if reddit_info['type'] == 'submission':
                result = self._search_submission(reddit_info['id'])
            elif reddit_info['type'] == 'comment':
                result = self._search_comment(reddit_info['id'])
            else:
                return RecoveryResult.failure_result(
                    f"Unsupported Reddit content type: {reddit_info['type']}",
                    RecoverySource.PULLPUSH_IO
                )

            duration = time.time() - start_time

            if result:
                return self._create_success_result(url, result, duration)
            else:
                return RecoveryResult.failure_result(
                    f"Content not found in PullPush.io archive (search duration: {duration:.2f}s)",
                    RecoverySource.PULLPUSH_IO
                )

        except requests.exceptions.Timeout:
            duration = time.time() - start_time
            return RecoveryResult.failure_result(
                f"PullPush.io request timed out after {duration:.2f}s",
                RecoverySource.PULLPUSH_IO
            )
        except requests.exceptions.RequestException as e:
            duration = time.time() - start_time
            return RecoveryResult.failure_result(
                f"PullPush.io request failed: {str(e)} (duration: {duration:.2f}s)",
                RecoverySource.PULLPUSH_IO
            )
        except Exception as e:
            duration = time.time() - start_time
            self._logger.error(f"Unexpected error in PullPush.io recovery: {e}")
            return RecoveryResult.failure_result(
                f"Unexpected error: {str(e)} (duration: {duration:.2f}s)",
                RecoverySource.PULLPUSH_IO
            )

    def _parse_reddit_url(self, url: str) -> Optional[Dict[str, str]]:
        """Parse Reddit URL to extract content ID and type."""
        try:
            parsed = urlparse(url)
            if not any(domain in parsed.netloc.lower() for domain in ['reddit.com', 'redd.it']):
                return None

            path = parsed.path.lower()

            # Match comment URL patterns before their parent submission URLs.
            comment_match = re.search(r'/comments/[a-z0-9]+/[^/]+/([a-z0-9]+)', path)
            if comment_match:
                return {
                    'type': 'comment',
                    'id': comment_match.group(1),
                    'subreddit': self._extract_subreddit(path)
                }

            # Match submission URL patterns
            submission_patterns = [
                r'/r/[^/]+/comments/([a-z0-9]+)',  # /r/subreddit/comments/ID
                r'/comments/([a-z0-9]+)',          # /comments/ID
                r'/([a-z0-9]+)/?$'                 # /ID (for redd.it)
            ]

            for pattern in submission_patterns:
                match = re.search(pattern, path)
                if match:
                    return {
                        'type': 'submission',
                        'id': match.group(1),
                        'subreddit': self._extract_subreddit(path)
                    }

        except Exception as e:
            self._logger.debug(f"Failed to parse Reddit URL {url}: {e}")

        return None

    def _extract_subreddit(self, path: str) -> Optional[str]:
        """Extract subreddit name from Reddit path."""
        subreddit_match = re.search(r'/r/([^/]+)', path)
        return subreddit_match.group(1) if subreddit_match else None

    def _search_submission(self, submission_id: str) -> Optional[Dict[str, Any]]:
        """Search for a Reddit submission by ID."""
        try:
            params = {
                'ids': submission_id,
                'size': 1
            }

            response = self.session.get(
                self.submission_endpoint,
                params=params,
                timeout=self.timeout
            )
            response.raise_for_status()

            data = response.json()
            if data.get('data') and len(data['data']) > 0:
                submission = data['data'][0]
                self._logger.debug(f"Found submission {submission_id} in PullPush.io")
                return {
                    'type': 'submission',
                    'data': submission,
                    'id': submission_id
                }

        except (requests.exceptions.RequestException, ValueError) as e:
            self._logger.debug(f"Submission search failed: {e}")

        return None

    def fetch_metadata_by_ids(self, content_type: str, ids: Iterable[str]) -> Dict[str, Dict[str, Any]]:
        """Fetch a bounded batch of archived post or comment records by ID."""
        endpoint = {
            'posts': self.submission_endpoint,
            'comments': self.comment_endpoint,
        }.get(content_type)
        if not endpoint:
            raise ValueError(f'Unsupported PullPush content type: {content_type}')

        unique_ids = list(dict.fromkeys(str(item_id) for item_id in ids if item_id))
        records = {}

        for start in range(0, len(unique_ids), PULLPUSH_BATCH_SIZE):
            batch = unique_ids[start:start + PULLPUSH_BATCH_SIZE]
            if not rate_limit_manager.acquire('pullpush_io', timeout=self.timeout):
                self._logger.warning('PullPush metadata lookup deferred by the rate limiter')
                break

            try:
                response = self.session.get(
                    endpoint,
                    params={'ids': ','.join(batch), 'size': len(batch)},
                    timeout=self.timeout,
                )
                rate_limit_manager.report_response('pullpush_io', response.status_code)
                response.raise_for_status()
                payload = response.json()
            except (requests.exceptions.RequestException, ValueError) as error:
                self._logger.warning(f'PullPush metadata lookup failed: {error}')
                continue

            data = payload.get('data', []) if isinstance(payload, dict) else []
            if not isinstance(data, list):
                self._logger.warning('PullPush returned an unexpected response shape')
                continue

            for record in data:
                if isinstance(record, dict) and record.get('id'):
                    records[str(record['id'])] = record

        return records

    def _search_comment(self, comment_id: str) -> Optional[Dict[str, Any]]:
        """Search for a Reddit comment by ID."""
        try:
            params = {
                'ids': comment_id,
                'size': 1
            }

            response = self.session.get(
                self.comment_endpoint,
                params=params,
                timeout=self.timeout
            )
            response.raise_for_status()

            data = response.json()
            if data.get('data') and len(data['data']) > 0:
                comment = data['data'][0]
                self._logger.debug(f"Found comment {comment_id} in PullPush.io")
                return {
                    'type': 'comment',
                    'data': comment,
                    'id': comment_id
                }

        except (requests.exceptions.RequestException, ValueError) as e:
            self._logger.debug(f"Comment search failed: {e}")

        return None

    def _create_success_result(self, original_url: str, pullpush_data: Dict[str, Any],
                             duration: float) -> RecoveryResult:
        """Create a metadata-only recovery result from archived Reddit text."""

        content_data = pullpush_data['data']
        content_type = pullpush_data['type']
        content_id = pullpush_data['id']

        metadata = RecoveryMetadata(
            source=RecoverySource.PULLPUSH_IO,
            recovered_url=None,
            recovery_timestamp=time.time(),
            content_quality=RecoveryQuality.METADATA_ONLY,
            attempt_duration=duration,
            additional_metadata={
                'content_type': content_type,
                'content_id': content_id,
                'subreddit': content_data.get('subreddit'),
                'author': content_data.get('author'),
                'created_utc': content_data.get('created_utc'),
                'score': content_data.get('score'),
                'title': content_data.get('title'),
                'selftext': content_data.get('selftext'),
                'body': content_data.get('body'),
                'archived_data_available': True
            }
        )

        return RecoveryResult.success_result(None, metadata)

    def get_provider_info(self) -> Dict[str, Any]:
        """Get information about this recovery provider."""
        return {
            'name': 'PullPush.io',
            'source': RecoverySource.PULLPUSH_IO,
            'description': 'PullPush.io API - successor to Pushshift for Reddit data',
            'rate_limits': '15 req/min soft, 30 req/min hard, 1000 req/hour',
            'ethics': 'Controversial - facing legal challenges from Reddit',
            'reliability': 'Medium - operational but under legal pressure',
            'coverage': 'Reddit content only, comprehensive historical data'
        }
