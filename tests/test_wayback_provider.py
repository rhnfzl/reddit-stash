"""Wayback provider parsing, pinned against the shapes of its two APIs' answers."""

import time
import unittest
from unittest.mock import patch

from tests.recovery_helpers import RATE_LIMIT_TOKEN, WAYBACK, fake_response, fake_session
from utils.content_recovery.providers.wayback_provider import WaybackMachineProvider
from utils.content_recovery.recovery_metadata import RecoveryQuality

URL = 'https://example.com/a.jpg'
NOT_ARCHIVED = {'archived_snapshots': {}}
CDX_HEADER = ['urlkey', 'timestamp', 'original', 'mimetype', 'statuscode', 'digest', 'length']


def _availability(timestamp='20200102030405', status='200', available=True):
    return {'archived_snapshots': {'closest': {
        'available': available,
        'url': f'http://web.archive.org/web/{timestamp}/{URL}',
        'timestamp': timestamp,
        'status': status,
    }}}


class TestWaybackParsing(unittest.TestCase):
    def _recover(self, *payloads):
        provider = WaybackMachineProvider()
        provider.session = fake_session(*(fake_response(payload=payload) for payload in payloads))
        self.session = provider.session
        with patch(WAYBACK), patch(RATE_LIMIT_TOKEN, return_value=True):
            return provider.attempt_recovery(URL)

    def test_every_request_takes_a_rate_limit_token(self):
        provider = WaybackMachineProvider()
        provider.session = fake_session(fake_response(payload=NOT_ARCHIVED), fake_response(payload=[]))

        with patch(RATE_LIMIT_TOKEN, return_value=True) as cdx_token, patch(WAYBACK) as manager:
            provider.attempt_recovery(URL)

        self.assertEqual(provider.session.get.call_count, 2)
        self.assertEqual(manager.acquire.call_count, 1)
        self.assertEqual(cdx_token.call_count, 1)

    def test_cdx_token_timeout_after_a_429_reports_the_429(self):
        provider = WaybackMachineProvider()
        provider.session = fake_session(fake_response(429, reason='Too Many Requests'))
        logger = 'utils.content_recovery.providers.wayback_provider'

        with patch(RATE_LIMIT_TOKEN, return_value=False), patch(WAYBACK), \
                self.assertNoLogs(logger, level='ERROR'):
            result = provider.attempt_recovery(URL)

        self.assertFalse(result.success)
        self.assertIn('429', result.error_message)
        self.assertFalse(result.confirmed_miss)

    def test_available_snapshot_is_used_without_asking_cdx(self):
        result = self._recover(_availability())

        self.assertTrue(result.success)
        self.assertEqual(result.recovered_url, f'http://web.archive.org/web/20200102030405/{URL}')
        self.assertEqual(self.session.get.call_count, 1)
        details = result.metadata.additional_metadata
        self.assertEqual(details['wayback_timestamp'], '20200102030405')
        self.assertEqual(details['status_code'], '200')
        self.assertEqual(details['api_source'], 'availability')

    def test_cdx_row_fields_are_read_by_position(self):
        row = ['com,example)/a.jpg', '20200102030405', URL, 'image/jpeg', '200', 'DIGEST', '123']

        result = self._recover(NOT_ARCHIVED, [CDX_HEADER, row])

        self.assertTrue(result.success)
        self.assertEqual(result.recovered_url, f'https://web.archive.org/web/20200102030405/{URL}')
        details = result.metadata.additional_metadata
        self.assertEqual(details['mimetype'], 'image/jpeg')
        self.assertEqual(details['status_code'], '200')
        self.assertEqual(details['api_source'], 'cdx')

    def test_snapshot_marked_unavailable_falls_through_to_cdx(self):
        result = self._recover(_availability(available=False), [CDX_HEADER])

        self.assertFalse(result.success)
        self.assertEqual(self.session.get.call_count, 2)

    def test_not_archived_is_a_real_miss(self):
        result = self._recover(NOT_ARCHIVED, [CDX_HEADER])

        self.assertFalse(result.success)
        self.assertTrue(result.confirmed_miss)

    def test_malformed_answers_are_not_a_miss(self):
        result = self._recover({'unexpected': True}, {'unexpected': True})

        self.assertFalse(result.success)
        self.assertFalse(result.confirmed_miss)

    def test_quality_follows_capture_age_and_status(self):
        year = time.gmtime().tm_year
        for age, status, quality in (
            (0, '200', RecoveryQuality.HIGH_QUALITY),
            (3, '200', RecoveryQuality.MEDIUM_QUALITY),
            (4, '200', RecoveryQuality.LOW_QUALITY),
            (0, '301', RecoveryQuality.LOW_QUALITY),
        ):
            with self.subTest(age=age, status=status):
                result = self._recover(_availability(f'{year - age}0102030405', status))

                self.assertEqual(result.metadata.content_quality, quality)


if __name__ == '__main__':
    unittest.main()
