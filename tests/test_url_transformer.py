"""
Unit tests for URL transformer module.

Tests URL transformation functionality including platform detection,
URL preprocessing, and direct download URL generation.
"""

import unittest
import sys
import os

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.url_transformer import URLTransformer, TransformResult


class TestURLTransformer(unittest.TestCase):
    """Test cases for URL transformer functionality."""

    def setUp(self):
        """Set up test fixtures."""
        self.transformer = URLTransformer()

    def test_github_blob_to_raw_transformation(self):
        """Test GitHub blob URL transformation to raw URL."""
        test_cases = [
            {
                'input': 'https://github.com/user/repo/blob/main/file.txt',
                'expected': 'https://raw.githubusercontent.com/user/repo/main/file.txt',
                'platform': 'GitHub'
            },
            {
                'input': 'https://github.com/microsoft/vscode/blob/master/README.md',
                'expected': 'https://raw.githubusercontent.com/microsoft/vscode/master/README.md',
                'platform': 'GitHub'
            }
        ]

        for case in test_cases:
            with self.subTest(url=case['input']):
                result = self.transformer.transform(case['input'])
                self.assertTrue(result.transformed)
                self.assertEqual(result.url, case['expected'])
                self.assertEqual(result.platform, case['platform'])

    def test_reddit_media_wrapper_unwrap(self):
        """Test unwrapping reddit.com/media?url= to the direct asset URL."""
        test_cases = [
            {
                'input': 'https://www.reddit.com/media?url=https%3A%2F%2Fi.redd.it%2Fxyz.png',
                'expected': 'https://i.redd.it/xyz.png',
                'platform': 'Reddit Media'
            },
            {
                # Preserves inner query params (encoded & and =)
                'input': 'https://reddit.com/media?url=https%3A%2F%2Fpreview.redd.it%2Fabc.jpg%3Fwidth%3D640%26auto%3Dwebp',
                'expected': 'https://preview.redd.it/abc.jpg?width=640&auto=webp',
                'platform': 'Reddit Media'
            },
            {
                # `url` need not be the first query parameter
                'input': 'https://www.reddit.com/media?ref=share&url=https%3A%2F%2Fi.redd.it%2Fzzz.png',
                'expected': 'https://i.redd.it/zzz.png',
                'platform': 'Reddit Media'
            },
        ]

        for case in test_cases:
            with self.subTest(url=case['input']):
                result = self.transformer.transform(case['input'])
                self.assertTrue(result.transformed)
                self.assertEqual(result.url, case['expected'])
                self.assertEqual(result.platform, case['platform'])

    def test_reddit_direct_url_not_transformed(self):
        """A direct i.redd.it URL (not a /media wrapper) should pass through unchanged."""
        result = self.transformer.transform('https://i.redd.it/xyz.png')
        self.assertFalse(result.transformed)
        self.assertEqual(result.url, 'https://i.redd.it/xyz.png')

    def test_reddit_media_wrapper_rejects_lookalike_hosts(self):
        """The host-anchored pattern must not match non-reddit.com hosts.

        A bare substring match would let an attacker-controlled host smuggle an
        internal URL through the transformer (e.g. an SSRF target); the pattern
        is anchored to the reddit.com host to prevent that.
        """
        lookalikes = [
            'https://evil.example/reddit.com/media?url=http%3A%2F%2F169.254.169.254%2Flatest',
            'https://notreddit.com/media?url=https%3A%2F%2Fi.redd.it%2Fa.png',
            'https://reddit.com.evil.com/media?url=https%3A%2F%2Fi.redd.it%2Fa.png',
        ]
        for url in lookalikes:
            with self.subTest(url=url):
                result = self.transformer.transform(url)
                self.assertFalse(result.transformed)
                self.assertEqual(result.url, url)

    def test_gitlab_blob_to_raw_transformation(self):
        """Test GitLab blob URL transformation to raw URL."""
        test_cases = [
            {
                'input': 'https://gitlab.com/project/-/blob/main/file.txt',
                'expected': 'https://gitlab.com/project/-/raw/main/file.txt',
                'platform': 'GitLab'
            }
            # Self-hosted GitLab (gitlab.example.com) is intentionally unsupported:
            # the blob->raw transform hardcodes gitlab.com, and private instances
            # are vanishingly rare in Reddit-sourced links.
        ]

        for case in test_cases:
            with self.subTest(url=case['input']):
                result = self.transformer.transform(case['input'])
                self.assertTrue(result.transformed)
                self.assertEqual(result.url, case['expected'])
                self.assertEqual(result.platform, case['platform'])

    def test_bitbucket_src_to_raw_transformation(self):
        """Test Bitbucket src URL transformation to raw URL."""
        test_cases = [
            {
                'input': 'https://bitbucket.org/user/repo/src/main/file.txt',
                'expected': 'https://bitbucket.org/user/repo/raw/main/file.txt',
                'platform': 'Bitbucket'
            }
        ]

        for case in test_cases:
            with self.subTest(url=case['input']):
                result = self.transformer.transform(case['input'])
                self.assertTrue(result.transformed)
                self.assertEqual(result.url, case['expected'])
                self.assertEqual(result.platform, case['platform'])

    def test_dropbox_dl_parameter_transformation(self):
        """Test Dropbox dl=0 to dl=1 transformation."""
        test_cases = [
            {
                'input': 'https://www.dropbox.com/s/abc123/file.txt?dl=0',
                'expected': 'https://www.dropbox.com/s/abc123/file.txt?dl=1',
                'platform': 'Dropbox'
            },
            {
                'input': 'https://dropbox.com/s/xyz789/image.png?dl=0&other=param',
                'expected': 'https://dropbox.com/s/xyz789/image.png?dl=1&other=param',
                'platform': 'Dropbox'
            }
        ]

        for case in test_cases:
            with self.subTest(url=case['input']):
                result = self.transformer.transform(case['input'])
                self.assertTrue(result.transformed)
                self.assertEqual(result.url, case['expected'])
                self.assertEqual(result.platform, case['platform'])

    def test_pastebin_raw_transformation(self):
        """Test Pastebin URL transformation to raw URL."""
        test_cases = [
            {
                'input': 'https://pastebin.com/ABC123',
                'expected': 'https://pastebin.com/raw/ABC123',
                'platform': 'Pastebin'
            }
            # The pastebin.com/download/<id> form is intentionally unsupported
            # (rare in Reddit links); only the bare paste-id viewer is converted.
        ]

        for case in test_cases:
            with self.subTest(url=case['input']):
                result = self.transformer.transform(case['input'])
                self.assertTrue(result.transformed)
                self.assertEqual(result.url, case['expected'])
                self.assertEqual(result.platform, case['platform'])

    def test_no_transformation_needed(self):
        """Test URLs that don't need transformation."""
        test_cases = [
            'https://i.imgur.com/abc123.jpg',
            'https://i.redd.it/xyz789.png',
            'https://example.com/direct/file.pdf',
            'https://raw.githubusercontent.com/user/repo/main/file.txt',  # Already raw
            'https://gitlab.com/project/-/raw/main/file.txt'  # Already raw
        ]

        for url in test_cases:
            with self.subTest(url=url):
                result = self.transformer.transform(url)
                self.assertFalse(result.transformed)
                self.assertEqual(result.url, url)
                self.assertIsNone(result.platform)

    def test_github_gist_transformation(self):
        """Test GitHub Gist URL transformation."""
        test_cases = [
            {
                'input': 'https://gist.github.com/user/abc123',
                'expected': 'https://gist.githubusercontent.com/user/abc123/raw',
                'platform': 'GitHub Gist'
            }
        ]

        for case in test_cases:
            with self.subTest(url=case['input']):
                result = self.transformer.transform(case['input'])
                self.assertTrue(result.transformed)
                self.assertEqual(result.url, case['expected'])
                self.assertEqual(result.platform, case['platform'])

    def test_invalid_urls(self):
        """Test handling of invalid URLs."""
        invalid_urls = [
            '',
            None,
            'not-a-url',
            'javascript:alert("xss")',
            'ftp://example.com/file.txt'
        ]

        for url in invalid_urls:
            with self.subTest(url=url):
                result = self.transformer.transform(url)
                self.assertFalse(result.transformed)
                if url:
                    self.assertEqual(result.url, url)

    def test_case_insensitive_domain_matching(self):
        """Test that domain matching is case insensitive."""
        test_cases = [
            'https://GITHUB.com/user/repo/blob/main/file.txt',
            'https://GitHub.Com/user/repo/blob/main/file.txt',
            'https://GITLAB.COM/project/-/blob/main/file.txt'
        ]

        for url in test_cases:
            with self.subTest(url=url):
                result = self.transformer.transform(url)
                self.assertTrue(result.transformed)

    def test_postimages_transformation(self):
        """Test PostImages URL transformation."""
        test_cases = [
            {
                'input': 'https://postimg.cc/abc123',
                # Best-effort CDN path; the exact filename/extension needs page
                # parsing we deliberately avoid, so no extension is guessed.
                'expected': 'https://i.postimg.cc/abc123/',
                'platform': 'PostImages'
            }
        ]

        for case in test_cases:
            with self.subTest(url=case['input']):
                result = self.transformer.transform(case['input'])
                self.assertTrue(result.transformed)
                self.assertEqual(result.url, case['expected'])
                self.assertEqual(result.platform, case['platform'])

    def test_imgbb_transformation(self):
        """Test ImgBB URL transformation."""
        test_cases = [
            {
                'input': 'https://ibb.co/abc123',
                # Best-effort CDN path (no guessed extension, see PostImages).
                'expected': 'https://i.ibb.co/abc123/',
                'platform': 'ImgBB'
            }
        ]

        for case in test_cases:
            with self.subTest(url=case['input']):
                result = self.transformer.transform(case['input'])
                self.assertTrue(result.transformed)
                self.assertEqual(result.url, case['expected'])
                self.assertEqual(result.platform, case['platform'])

    def test_transform_result_structure(self):
        """Test TransformResult data structure."""
        # Test with transformation
        result = self.transformer.transform('https://github.com/user/repo/blob/main/file.txt')
        self.assertIsInstance(result, TransformResult)
        self.assertTrue(result.transformed)
        self.assertIsNotNone(result.url)
        self.assertIsNotNone(result.platform)

        # Test without transformation
        result = self.transformer.transform('https://example.com/file.txt')
        self.assertIsInstance(result, TransformResult)
        self.assertFalse(result.transformed)
        self.assertIsNone(result.platform)

    def test_edge_cases(self):
        """Test edge cases and corner conditions."""
        edge_cases = [
            # URLs with ports
            'https://github.com:443/user/repo/blob/main/file.txt',
            # URLs with unusual paths
            'https://github.com/user/repo/blob/feature/complex-branch-name/path/to/file.txt',
            # URLs with query parameters
            'https://github.com/user/repo/blob/main/file.txt?ref=abc123',
            # URLs with fragments
            'https://github.com/user/repo/blob/main/file.txt#L123'
        ]

        for url in edge_cases:
            with self.subTest(url=url):
                result = self.transformer.transform(url)
                # Should still transform GitHub URLs regardless of edge cases
                if 'github.com' in url and '/blob/' in url:
                    self.assertTrue(result.transformed)
                    self.assertIn('raw.githubusercontent.com', result.url)

    def test_transformation_preserves_query_params(self):
        """Test that transformations preserve important query parameters."""
        url = 'https://github.com/user/repo/blob/main/file.txt?ref=abc123&token=xyz'
        result = self.transformer.transform(url)

        self.assertTrue(result.transformed)
        # Query parameters should be preserved in the transformation
        self.assertIn('ref=abc123', result.url)
        self.assertIn('token=xyz', result.url)

    # NOTE: a custom-transformer registration API (register_transformer) was
    # never implemented and had no callers; its aspirational test was removed
    # rather than build an unused plugin mechanism.


if __name__ == '__main__':
    unittest.main()