"""
PullPush.io provider for content recovery.

This module implements content recovery using PullPush.io, a successor to
Pushshift that archives Reddit data. Note that this service faces legal
challenges from Reddit and has strict rate limits.

Configured request limits:
- Soft limit: 15 requests/minute
- Hard limit: 30 requests/minute
- Long-term limit: 1000 requests/hour

API Documentation: https://pullpush.io/
"""

import time
import requests
import logging
from typing import Any
from collections.abc import Iterable
from urllib.parse import urlparse
import re

from utils.content_recovery.providers.archive_session import archive_session
from utils.content_recovery.recovery_metadata import ArchiveLookupError, RecoveryResult, RecoveryMetadata, RecoverySource, RecoveryQuality
from utils.domain_matching import domain_matches
from utils.rate_limiter import parse_retry_after, rate_limit_manager


PULLPUSH_BASE_URL = 'https://api.pullpush.io'
PULLPUSH_BATCH_SIZE = 100


def _archive_records(payload: Any) -> list:
    """Return the record list; a 200 without one answered nothing, so it must not read as a miss."""
    records = payload.get('data') if isinstance(payload, dict) else None
    if not isinstance(records, list):
        raise ValueError('unexpected PullPush response shape')
    return records


class PullPushProvider:
    """Provider for enriching Reddit recovery with PullPush archive metadata."""

    def __init__(self, timeout: int = 10):
        self.timeout = timeout
        self.session = archive_session()
        self._logger = logging.getLogger(f"{__name__}.{self.__class__.__name__}")
        self.submission_endpoint = f"{PULLPUSH_BASE_URL}/reddit/search/submission"
        self.comment_endpoint = f"{PULLPUSH_BASE_URL}/reddit/search/comment"

    def attempt_recovery(self, url: str) -> RecoveryResult:
        """
        Attempt to recover Reddit content from PullPush.io.

        Args:
            url: Original Reddit URL to recover

        Returns:
            RecoveryResult with archived metadata when a matching record exists; every
            failure, including a rate limit wait that ran out, is a result rather than a raise
        """
        if not rate_limit_manager.acquire('pullpush_io', timeout=45):
            return RecoveryResult.failure_result(
                'pullpush_io recovery deferred by rate limiter',
                RecoverySource.PULLPUSH_IO,
            )

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
                    RecoverySource.PULLPUSH_IO,
                    confirmed_miss=True,
                )

        except (requests.exceptions.RequestException, ValueError) as e:
            self._logger.debug(f"PullPush.io lookup failed: {e}")
            return RecoveryResult.failure_from_exception(e, RecoverySource.PULLPUSH_IO, start_time)
        except Exception as e:
            self._logger.error(f"Unexpected error in PullPush.io recovery: {e}")
            return RecoveryResult.failure_from_exception(e, RecoverySource.PULLPUSH_IO, start_time)

    def _parse_reddit_url(self, url: str) -> dict[str, str] | None:
        """Parse Reddit URL to extract content ID and type."""
        try:
            parsed = urlparse(url)
            if not any(domain_matches(parsed.hostname, domain) for domain in ('reddit.com', 'redd.it')):
                return None

            path = parsed.path.lower()

            # Match comment URL patterns before their parent submission URLs.
            comment_match = re.search(r'/comments/[a-z0-9]+/[^/]+/([a-z0-9]+)', path)
            if comment_match:
                return {
                    'type': 'comment',
                    'id': comment_match.group(1),
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
                    }

        except Exception as e:
            self._logger.debug(f"Failed to parse Reddit URL {url}: {e}")

        return None

    def _search_submission(self, submission_id: str) -> dict[str, Any] | None:
        """Return the archived submission, or None when the archive has no record of it."""
        response = self.session.get(
            self.submission_endpoint,
            params={'ids': submission_id, 'size': 1},
            timeout=self.timeout
        )
        rate_limit_manager.report_response('pullpush_io', response.status_code, parse_retry_after(response))
        response.raise_for_status()

        records = _archive_records(response.json())
        if not records:
            return None

        self._logger.debug(f"Found submission {submission_id} in PullPush.io")
        return {'type': 'submission', 'data': records[0], 'id': submission_id}

    def fetch_posts(self, ids: Iterable[str]) -> dict[str, dict[str, Any]]:
        """Return archived posts by ID, or raise ArchiveLookupError carrying what was found."""
        return self._fetch_by_ids(self.submission_endpoint, ids)

    def fetch_comments(self, ids: Iterable[str]) -> dict[str, dict[str, Any]]:
        """Return archived comments by ID, or raise ArchiveLookupError carrying what was found."""
        return self._fetch_by_ids(self.comment_endpoint, ids)

    def _fetch_by_ids(self, endpoint: str, ids: Iterable[str]) -> dict[str, dict[str, Any]]:
        unique_ids = list(dict.fromkeys(str(item_id) for item_id in ids if item_id))
        records = {}
        failure = None

        for start in range(0, len(unique_ids), PULLPUSH_BATCH_SIZE):
            batch = unique_ids[start:start + PULLPUSH_BATCH_SIZE]
            if not rate_limit_manager.acquire('pullpush_io', timeout=self.timeout):
                self._logger.warning('PullPush metadata lookup deferred by the rate limiter')
                failure = 'deferred by the rate limiter'
                break

            try:
                response = self.session.get(
                    endpoint,
                    params={'ids': ','.join(batch), 'size': len(batch)},
                    timeout=self.timeout,
                )
                rate_limit_manager.report_response('pullpush_io', response.status_code, parse_retry_after(response))
                response.raise_for_status()
                data = _archive_records(response.json())
            except (requests.exceptions.RequestException, ValueError) as error:
                self._logger.warning(f'PullPush metadata lookup failed: {error}')
                failure = str(error)
                continue

            for record in data:
                if isinstance(record, dict) and record.get('id'):
                    records[str(record['id'])] = record
                else:
                    failure = 'unusable record in response'

        if failure:
            raise ArchiveLookupError(f'PullPush lookup: {failure}', records)
        return records

    def _search_comment(self, comment_id: str) -> dict[str, Any] | None:
        """Return the archived comment, or None when the archive has no record of it."""
        response = self.session.get(
            self.comment_endpoint,
            params={'ids': comment_id, 'size': 1},
            timeout=self.timeout
        )
        rate_limit_manager.report_response('pullpush_io', response.status_code, parse_retry_after(response))
        response.raise_for_status()

        records = _archive_records(response.json())
        if not records:
            return None

        self._logger.debug(f"Found comment {comment_id} in PullPush.io")
        return {'type': 'comment', 'data': records[0], 'id': comment_id}

    def _create_success_result(self, original_url: str, pullpush_data: dict[str, Any],
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
