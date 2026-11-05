"""Parallel gallery downloads must share DNS lookups, or a resolver's rate limit fails them."""

import socket
import threading
import time
import unittest
from unittest.mock import patch

from utils.media_services.url_security import URLSecurityValidator

PUBLIC = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('151.101.1.140', 0))]
PRIVATE = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('10.0.0.5', 0))]
LOOKUP = 'utils.media_services.url_security.socket.getaddrinfo'
URL = 'https://preview.redd.it/abc.jpeg'


class TestDnsLookupCache(unittest.TestCase):
    def test_parallel_checks_of_one_host_make_one_lookup(self):
        validator = URLSecurityValidator()
        barrier = threading.Barrier(10)
        results = []

        def check():
            barrier.wait()
            results.append(validator.is_safe_for_download(URL))
            results.append(bool(validator.resolve_public_addresses(URL)))

        def slow_lookup(*_args, **_kwargs):
            time.sleep(0.05)
            return PUBLIC

        with patch(LOOKUP, side_effect=slow_lookup) as lookup:
            threads = [threading.Thread(target=check) for _ in range(10)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

        self.assertEqual(lookup.call_count, 1)
        self.assertEqual(results, [True] * 20)

    def test_failed_lookup_is_not_remembered(self):
        validator = URLSecurityValidator()
        with patch(LOOKUP, side_effect=[socket.gaierror('dropped'), PUBLIC]):
            self.assertFalse(validator.is_safe_for_download(URL))
            self.assertTrue(validator.is_safe_for_download(URL))

    def test_remembered_private_address_is_still_rejected(self):
        validator = URLSecurityValidator()
        with patch(LOOKUP, return_value=PRIVATE) as lookup:
            self.assertFalse(validator.is_safe_for_download(URL))
            self.assertFalse(validator.is_safe_for_download(URL))
            self.assertEqual(validator.resolve_public_addresses(URL), ())
        self.assertEqual(lookup.call_count, 1)

    def test_lookup_runs_again_after_the_entry_expires(self):
        validator = URLSecurityValidator()
        with patch(LOOKUP, side_effect=[PUBLIC, PRIVATE]), \
                patch('utils.media_services.url_security.time.monotonic', side_effect=[0.0, 0.0, 1000.0, 1000.0]):
            self.assertTrue(validator.is_safe_for_download(URL))
            self.assertFalse(validator.is_safe_for_download(URL))


if __name__ == '__main__':
    unittest.main()
