"""Small, rate-aware client for Arctic Shift ID lookups."""

import logging
from collections.abc import Iterable

import requests

from utils.content_recovery.recovery_metadata import ArchiveLookupError
from utils.rate_limiter import parse_retry_after, rate_limit_manager


ARCTIC_SHIFT_BASE_URL = 'https://arctic-shift.photon-reddit.com/api'
ARCTIC_SHIFT_SERVICE = 'arctic_shift'
ARCTIC_SHIFT_BATCH_SIZE = 500

POST_FIELDS = ('id', 'title', 'selftext', 'subreddit', 'author', 'created_utc', 'score')
COMMENT_FIELDS = ('id', 'body', 'link_id', 'parent_id', 'subreddit', 'author', 'created_utc', 'score')


class ArcticShiftClient:
    """Fetch archived Reddit posts and comments by ID in bounded batches."""

    def __init__(self, timeout: int = 10, session=None, batch_size: int = ARCTIC_SHIFT_BATCH_SIZE):
        self.timeout = timeout
        self.session = session or requests.Session()
        self.batch_size = min(max(1, batch_size), ARCTIC_SHIFT_BATCH_SIZE)
        self._logger = logging.getLogger(f'{__name__}.{self.__class__.__name__}')
        self.session.headers.setdefault('User-Agent', 'Reddit Stash Content Recovery/1.0')
        self.session.headers.setdefault('Accept', 'application/json')

    def fetch_posts(self, ids: Iterable[str]) -> dict[str, dict]:
        """Return archived posts by base36 Reddit ID, or raise ArchiveLookupError."""
        return self._fetch_by_ids('posts', ids, POST_FIELDS)

    def fetch_comments(self, ids: Iterable[str]) -> dict[str, dict]:
        """Return archived comments by base36 Reddit ID, or raise ArchiveLookupError."""
        return self._fetch_by_ids('comments', ids, COMMENT_FIELDS)

    def _fetch_by_ids(self, content_type: str, ids: Iterable[str], fields: tuple[str, ...]) -> dict[str, dict]:
        normalized_ids = self._normalize_ids(ids)
        records = {}
        failure = None

        for batch in self._batches(normalized_ids):
            if not rate_limit_manager.acquire(ARCTIC_SHIFT_SERVICE, timeout=self.timeout):
                self._logger.warning('Arctic Shift request deferred by the rate limiter')
                failure = 'deferred by the rate limiter'
                break

            try:
                response = self.session.get(
                    f'{ARCTIC_SHIFT_BASE_URL}/{content_type}/ids',
                    params={'ids': ','.join(batch), 'fields': ','.join(fields)},
                    timeout=self.timeout,
                )
                rate_limit_manager.report_response(
                    ARCTIC_SHIFT_SERVICE,
                    response.status_code,
                    parse_retry_after(response),
                )
                response.raise_for_status()
                payload = response.json()
            except (requests.RequestException, ValueError) as error:
                self._logger.warning(f'Arctic Shift {content_type} lookup failed: {error}')
                failure = str(error)
                continue

            # A 200 without a record list answered nothing, so it must not read as a miss.
            data = payload.get('data') if isinstance(payload, dict) else None
            if not isinstance(data, list):
                self._logger.warning('Arctic Shift returned an unexpected response shape')
                failure = 'unexpected response shape'
                continue

            for record in data:
                if isinstance(record, dict) and record.get('id'):
                    records[self._normalize_id(str(record['id']))] = record
                else:
                    failure = 'unusable record in response'

        if failure:
            raise ArchiveLookupError(f'Arctic Shift {content_type} lookup: {failure}', records)
        return records

    @staticmethod
    def _normalize_ids(ids: Iterable[str]) -> list[str]:
        seen = set()
        normalized_ids = []
        for item_id in ids:
            normalized_id = ArcticShiftClient._normalize_id(str(item_id))
            if normalized_id and normalized_id not in seen:
                seen.add(normalized_id)
                normalized_ids.append(normalized_id)
        return normalized_ids

    @staticmethod
    def _normalize_id(item_id: str) -> str:
        normalized_id = item_id.strip()
        if normalized_id.startswith(('t1_', 't3_')):
            normalized_id = normalized_id[3:]
        return normalized_id

    def _batches(self, ids: list[str]):
        for start in range(0, len(ids), self.batch_size):
            yield ids[start:start + self.batch_size]

