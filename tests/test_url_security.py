"""
Unit tests for URL security validator module.

Tests URL validation, security checks, domain blocklisting,
and malicious URL detection functionality.
"""

import unittest
import sys
import os
from urllib.parse import urlparse

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.url_security import URLSecurityValidator, ValidationResult, validate_url, is_safe_url


class TestURLSecurityValidator(unittest.TestCase):
    """Test cases for URL security validator functionality."""

    def setUp(self):
        """Set up test fixtures."""
        self.validator = URLSecurityValidator()

    def test_valid_https_urls(self):
        """Test validation of valid HTTPS URLs."""
        valid_urls = [
            'https://example.com',
            'https://www.reddit.com/r/python',
            'https://github.com/user/repo',
            'https://i.imgur.com/abc123.jpg'
        ]

        for url in valid_urls:
            with self.subTest(url=url):
                result = self.validator.validate_url(url)
                self.assertTrue(result.is_valid)
                self.assertEqual(result.risk_level, 'low')
                self.assertIsNotNone(result.cleaned_url)

    def test_valid_http_urls(self):
        """Test validation of valid HTTP URLs."""
        valid_urls = [
            'http://example.com',
            'http://archive.org/web/abc123'
        ]

        for url in valid_urls:
            with self.subTest(url=url):
                result = self.validator.validate_url(url)
                self.assertTrue(result.is_valid)
                self.assertEqual(result.risk_level, 'low')

    def test_invalid_schemes(self):
        """Test rejection of invalid URL schemes."""
        invalid_urls = [
            'javascript:alert("xss")',
            'data:text/html,<script>alert("xss")</script>',
            'file:///etc/passwd',
            'ftp://example.com/file.txt'
        ]

        for url in invalid_urls:
            with self.subTest(url=url):
                result = self.validator.validate_url(url)
                self.assertFalse(result.is_valid)
                self.assertEqual(result.risk_level, 'high')
                self.assertIn('Invalid scheme', str(result.issues))

    def test_blocked_domains(self):
        """Test blocking of malicious domains."""
        blocked_urls = [
            'https://localhost/file.txt',
            'https://127.0.0.1/api/data',
            'https://0.0.0.0/exploit',
            'http://malware.com/file.exe'
        ]

        for url in blocked_urls:
            with self.subTest(url=url):
                result = self.validator.validate_url(url)
                self.assertFalse(result.is_valid)
                self.assertEqual(result.risk_level, 'high')
                self.assertIn('blocked', str(result.issues).lower())

    def test_private_ip_addresses(self):
        """Test blocking of private/internal IP addresses."""
        private_ips = [
            'https://192.168.1.1/admin',
            'https://10.0.0.1/config',
            'https://172.16.0.1/internal',
            'https://169.254.1.1/metadata'
        ]

        for url in private_ips:
            with self.subTest(url=url):
                result = self.validator.validate_url(url)
                self.assertFalse(result.is_valid)
                self.assertEqual(result.risk_level, 'high')
                self.assertIn('Private/internal IP', str(result.issues))

    def test_suspicious_patterns(self):
        """Test detection of suspicious URL patterns."""
        suspicious_urls = [
            'https://example.com/file.txt?path=../../../etc/passwd',
            'https://example.com/search?q=<script>alert("xss")</script>',
            'https://example.com/data?url=file:///etc/passwd'
        ]

        for url in suspicious_urls:
            with self.subTest(url=url):
                result = self.validator.validate_url(url)
                self.assertFalse(result.is_valid)
                self.assertEqual(result.risk_level, 'high')
                self.assertIn('Suspicious pattern', str(result.issues))

    def test_url_length_validation(self):
        """Test URL length validation."""
        # Test very short URL
        short_url = 'http://a'
        result = self.validator.validate_url(short_url)
        self.assertFalse(result.is_valid)
        self.assertIn('too short', str(result.issues))

        # Test very long URL
        long_url = 'https://example.com/' + 'a' * 3000
        result = self.validator.validate_url(long_url)
        self.assertFalse(result.is_valid)
        self.assertIn('too long', str(result.issues))

    def test_empty_and_invalid_inputs(self):
        """Test handling of empty and invalid inputs."""
        invalid_inputs = [
            '',
            None,
            123,
            [],
            {}
        ]

        for invalid_input in invalid_inputs:
            with self.subTest(input=invalid_input):
                result = self.validator.validate_url(invalid_input)
                self.assertFalse(result.is_valid)
                self.assertEqual(result.risk_level, 'high')

    def test_malformed_urls(self):
        """Test handling of malformed URLs."""
        malformed_urls = [
            'https://',
            'https://.',
            'https://...',
            'https://user@:password@example.com',
            'https://exam ple.com/file.txt'  # Space in domain
        ]

        for url in malformed_urls:
            with self.subTest(url=url):
                result = self.validator.validate_url(url)
                self.assertFalse(result.is_valid)
                self.assertGreater(len(result.issues), 0)

    def test_domain_format_validation(self):
        """Test domain format validation."""
        invalid_domains = [
            'https://exam..ple.com/file.txt',  # Double dots
            'https://example-.com/file.txt',   # Ending with hyphen
            'https://-example.com/file.txt',   # Starting with hyphen
            'https://exam_ple.com/file.txt'    # Underscore in domain
        ]

        for url in invalid_domains:
            with self.subTest(url=url):
                result = self.validator.validate_url(url)
                self.assertFalse(result.is_valid)
                self.assertTrue(any('domain' in issue.lower() for issue in result.issues))

    def test_url_cleaning(self):
        """Test URL cleaning and normalization."""
        test_cases = [
            {
                'input': 'HTTPS://EXAMPLE.COM/File.txt',
                'expected_scheme': 'https',
                'expected_domain': 'example.com'
            },
            {
                'input': 'https://example.com/path?param=value#fragment',
                'expected_no_fragment': True
            }
        ]

        for case in test_cases:
            with self.subTest(url=case['input']):
                result = self.validator.validate_url(case['input'])
                self.assertTrue(result.is_valid)
                self.assertIsNotNone(result.cleaned_url)
                parsed = urlparse(result.cleaned_url)
                if 'expected_scheme' in case:
                    self.assertEqual(parsed.scheme, case['expected_scheme'])
                if 'expected_domain' in case:
                    self.assertEqual(parsed.hostname, case['expected_domain'])
                if case.get('expected_no_fragment'):
                    self.assertEqual(parsed.fragment, '')

    def test_add_remove_blocked_domains(self):
        """Test adding and removing blocked domains."""
        test_domain = 'malicious.example.com'
        test_url = f'https://{test_domain}/file.txt'

        # Initially should be valid
        result = self.validator.validate_url(test_url)
        self.assertTrue(result.is_valid)

        # Add to blocked domains
        self.validator.add_blocked_domain(test_domain)
        result = self.validator.validate_url(test_url)
        self.assertFalse(result.is_valid)
        self.assertIn('blocked', str(result.issues).lower())

        # Remove from blocked domains
        removed = self.validator.remove_blocked_domain(test_domain)
        self.assertTrue(removed)
        result = self.validator.validate_url(test_url)
        self.assertTrue(result.is_valid)

        # Try to remove non-existent domain
        removed = self.validator.remove_blocked_domain('nonexistent.com')
        self.assertFalse(removed)

    def test_case_insensitive_blocking(self):
        """Test that domain blocking is case insensitive."""
        self.validator.add_blocked_domain('MALICIOUS.COM')

        test_urls = [
            'https://malicious.com/file.txt',
            'https://MALICIOUS.COM/file.txt',
            'https://Malicious.Com/file.txt'
        ]

        for url in test_urls:
            with self.subTest(url=url):
                result = self.validator.validate_url(url)
                self.assertFalse(result.is_valid)

    def test_is_safe_for_download(self):
        """Test the is_safe_for_download convenience method."""
        safe_urls = [
            'https://example.com/file.txt',
            'https://github.com/user/repo/file.py'
        ]

        unsafe_urls = [
            'javascript:alert("xss")',
            'https://localhost/file.txt',
            'https://malware.com/trojan.exe'
        ]

        for url in safe_urls:
            with self.subTest(url=url):
                self.assertTrue(self.validator.is_safe_for_download(url))

        for url in unsafe_urls:
            with self.subTest(url=url):
                self.assertFalse(self.validator.is_safe_for_download(url))

    def test_security_report_generation(self):
        """Test detailed security report generation."""
        test_url = 'https://example.com/file.txt'
        report = self.validator.get_security_report(test_url)

        self.assertIsInstance(report, dict)
        self.assertEqual(report['url'], test_url)
        self.assertIn('is_valid', report)
        self.assertIn('risk_level', report)
        self.assertIn('issues', report)
        self.assertIn('timestamp', report)

        if report['is_valid']:
            self.assertIn('analysis', report)
            analysis = report['analysis']
            self.assertIn('scheme', analysis)
            self.assertIn('domain', analysis)
            self.assertIn('domain_blocked', analysis)

    def test_global_validator_functions(self):
        """Test global convenience functions."""
        # Test validate_url function
        result = validate_url('https://example.com')
        self.assertIsInstance(result, ValidationResult)
        self.assertTrue(result.is_valid)

        # Test is_safe_url function
        self.assertTrue(is_safe_url('https://example.com'))
        self.assertFalse(is_safe_url('javascript:alert("xss")'))

    def test_custom_blocked_domains_initialization(self):
        """Test initialization with custom blocked domains."""
        custom_domains = {'bad.example.com', 'evil.test.org'}
        validator = URLSecurityValidator(additional_blocked_domains=custom_domains)

        for domain in custom_domains:
            url = f'https://{domain}/file.txt'
            result = validator.validate_url(url)
            self.assertFalse(result.is_valid)
            self.assertIn('blocked', str(result.issues).lower())

    def test_medium_risk_url_is_rejected(self):
        """Test that an invalid domain is rejected with medium risk."""
        result = self.validator.validate_url('https://example-.com/file.txt')
        self.assertFalse(result.is_valid)
        self.assertEqual(result.risk_level, 'medium')
        self.assertTrue(any('domain' in issue.lower() for issue in result.issues))

    def test_url_with_ports(self):
        """Test URLs with explicit ports."""
        urls_with_ports = [
            'https://example.com:443/file.txt',
            'http://example.com:80/file.txt',
            'https://api.example.com:8443/data'
        ]

        for url in urls_with_ports:
            with self.subTest(url=url):
                result = self.validator.validate_url(url)
                self.assertTrue(result.is_valid)

    def test_unicode_domains(self):
        """Test handling of internationalized domain names."""
        unicode_urls = [
            'https://xn--nxasmq6b.example/file.txt',  # Punycode
            'https://测试.example.com/file.txt'  # Unicode domain
        ]

        for url in unicode_urls:
            with self.subTest(url=url):
                result = self.validator.validate_url(url)
                # Should handle gracefully (either accept or reject cleanly)
                self.assertIsInstance(result, ValidationResult)

    def test_edge_case_schemes(self):
        """Test edge cases with scheme validation."""
        edge_cases = [
            'HTTP://example.com',  # Uppercase
            'Https://example.com', # Mixed case
            'https ://example.com', # Space (should be invalid)
            'https://example.com',  # Normal case
        ]

        for url in edge_cases:
            with self.subTest(url=url):
                result = self.validator.validate_url(url)
                if ' ' in url:
                    self.assertFalse(result.is_valid)
                    self.assertGreater(len(result.issues), 0)
                else:
                    self.assertTrue(result.is_valid)
                    self.assertEqual(result.cleaned_url, url.lower())


class TestValidationResult(unittest.TestCase):
    """Test cases for ValidationResult data structure."""

    def test_validation_result_creation(self):
        """Test ValidationResult creation and default values."""
        result = ValidationResult(is_valid=True)
        self.assertTrue(result.is_valid)
        self.assertIsNone(result.cleaned_url)
        self.assertEqual(result.issues, [])
        self.assertEqual(result.risk_level, "low")

    def test_validation_result_with_issues(self):
        """Test ValidationResult with issues."""
        issues = ["Test issue 1", "Test issue 2"]
        result = ValidationResult(
            is_valid=False,
            issues=issues,
            risk_level="high"
        )
        self.assertFalse(result.is_valid)
        self.assertEqual(result.issues, issues)
        self.assertEqual(result.risk_level, "high")


if __name__ == '__main__':
    unittest.main()
