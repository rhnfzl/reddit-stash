"""Tests for hostname boundary matching."""

import unittest

from utils.domain_matching import domain_matches


class TestDomainMatches(unittest.TestCase):
    def test_matches_exact_hosts_and_subdomains(self):
        self.assertTrue(domain_matches('i.redd.it', 'i.redd.it'))
        self.assertTrue(domain_matches('cache.i.redd.it', 'i.redd.it'))

    def test_rejects_suffix_lookalikes(self):
        self.assertFalse(domain_matches('i.redd.it.attacker.example', 'i.redd.it'))
        self.assertFalse(domain_matches('notimgur.com', 'imgur.com'))


if __name__ == '__main__':
    unittest.main()
