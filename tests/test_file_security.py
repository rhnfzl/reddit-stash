"""
Unit tests for path security module.

Tests path traversal prevention, path sanitization,
and secure file operations functionality.
"""

import unittest
import tempfile
import os
import sys
# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.path_security import (
    SecurePathHandler, PathValidationResult,
    create_safe_path, create_reddit_file_path
)


class TestSecurePathHandler(unittest.TestCase):
    """Test cases for secure path handler functionality."""

    def setUp(self):
        """Set up test fixtures."""
        self.handler = SecurePathHandler()
        self.temp_dir = tempfile.mkdtemp()
        self.base_dir = os.path.realpath(self.temp_dir)

    def tearDown(self):
        """Clean up test fixtures."""
        import shutil
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir)

    def assert_path_within_base(self, path):
        self.assertEqual(
            os.path.commonpath([os.path.realpath(path), self.base_dir]),
            self.base_dir,
        )

    def test_safe_path_component_sanitization(self):
        """Test sanitization of individual path components."""
        test_cases = [
            {
                'input': 'normal_filename.txt',
                'expected_safe': True,
                'expected_output': 'normal_filename.txt'
            },
            {
                'input': 'file with spaces.txt',
                'expected_safe': True,
                'expected_output': 'file with spaces.txt'
            },
            {
                'input': 'file<>:"/\\|?*.txt',  # Dangerous characters
                'expected_safe': True,
                'expected_output': 'file_.txt'  # Dangerous characters are coalesced
            },
            {
                'input': '..traversal',
                'expected_safe': False  # Should be rejected
            },
            {
                'input': '../../../etc/passwd',
                'expected_safe': False  # Should be rejected
            },
            {
                'input': 'CON',  # Reserved Windows name
                'expected_safe': True,
                'expected_output': '_CON'  # Should be prefixed
            }
        ]

        for case in test_cases:
            with self.subTest(input=case['input']):
                result = self.handler.sanitize_path_component(case['input'])
                self.assertEqual(result.is_safe, case['expected_safe'])
                if case['expected_safe'] and 'expected_output' in case:
                    self.assertEqual(result.sanitized_component, case['expected_output'])

    def test_path_traversal_prevention(self):
        """Test prevention of directory traversal attacks."""
        dangerous_components = [
            '..',
            '../',
            '..\\',
            '../../etc/passwd',
            'file/../../../etc/passwd',
            '..\\..\\..',
            '%2e%2e%2f',  # URL encoded
        ]

        for component in dangerous_components:
            with self.subTest(component=component):
                result = self.handler.sanitize_path_component(component)
                self.assertFalse(result.is_safe)
                self.assertIn('traversal', str(result.issues).lower())

    def test_reserved_filename_handling(self):
        """Test handling of Windows reserved filenames."""
        reserved_names = [
            'CON', 'PRN', 'AUX', 'NUL',
            'COM1', 'COM2', 'LPT1', 'LPT9', 'CON.', 'CON .', 'CON.txt', 'LPT1.log'
        ]

        for name in reserved_names:
            with self.subTest(name=name):
                result = self.handler.sanitize_path_component(name)
                self.assertTrue(result.is_safe)
                self.assertTrue(result.sanitized_component.startswith('_'))
                self.assertIn('Reserved filename', str(result.issues))

    def test_create_safe_path_success(self):
        """Test successful creation of safe paths."""
        test_cases = [
            ['normal_file.txt'],
            ['subdir', 'file.txt'],
            ['reddit_posts', 'POST_abc123.md'],
            ['deep', 'nested', 'path', 'file.txt']
        ]

        for components in test_cases:
            with self.subTest(components=components):
                result = self.handler.create_safe_path(self.base_dir, *components)
                self.assertTrue(result.is_safe)
                self.assertIsNotNone(result.safe_path)
                self.assert_path_within_base(result.safe_path)

    def test_create_safe_path_traversal_prevention(self):
        """Test that create_safe_path prevents directory traversal."""
        dangerous_cases = [
            ['..', 'etc', 'passwd'],
            ['normal', '..', '..', '..', 'etc', 'passwd'],
            ['../../../etc/passwd'],
            ['/absolute/path/attack'],
        ]

        for components in dangerous_cases:
            with self.subTest(components=components):
                result = self.handler.create_safe_path(self.base_dir, *components)
                # Should either be invalid or stay within base directory
                if result.is_safe:
                    self.assert_path_within_base(result.safe_path)
                else:
                    self.assertGreater(len(result.issues), 0)

    def test_path_within_base_validation(self):
        """Test validation that paths stay within base directory."""
        # Create a legitimate subdirectory
        safe_subdir = os.path.join(self.base_dir, 'safe_subdir')
        os.makedirs(safe_subdir)

        # Test paths within base
        safe_paths = [
            safe_subdir,
            os.path.join(self.base_dir, 'new_file.txt'),
            os.path.join(safe_subdir, 'nested_file.txt')
        ]

        for path in safe_paths:
            with self.subTest(path=path):
                result = self.handler.validate_existing_path(path, self.base_dir)
                self.assertTrue(result.is_safe)

        # Test path outside base (using parent directory)
        outside_path = os.path.dirname(self.base_dir)
        result = self.handler.validate_existing_path(outside_path, self.base_dir)
        self.assertFalse(result.is_safe)
        self.assertIn('outside base directory', str(result.issues))

    def test_reddit_file_path_creation(self):
        """Test creation of Reddit-specific file paths."""
        test_cases = [
            {
                'subreddit': 'python',
                'content_type': 'POST',
                'content_id': 'abc123',
                'expected_filename': 'POST_abc123.md'
            },
            {
                'subreddit': 'programming',
                'content_type': 'COMMENT',
                'content_id': 'xyz789',
                'expected_filename': 'COMMENT_xyz789.md'
            },
            {
                'subreddit': 'askreddit',
                'content_type': 'SAVED_POST',
                'content_id': 'def456',
                'expected_filename': 'SAVED_POST_def456.md'
            }
        ]

        for case in test_cases:
            with self.subTest(case=case):
                result = self.handler.create_reddit_file_path(
                    self.base_dir,
                    case['subreddit'],
                    case['content_type'],
                    case['content_id']
                )
                self.assertTrue(result.is_safe)
                self.assertIsNotNone(result.safe_path)
                self.assertIn(case['expected_filename'], result.safe_path)
                self.assertIn(case['subreddit'], result.safe_path)

    def test_reddit_file_path_invalid_content_type(self):
        """Test Reddit file path creation with invalid content types."""
        invalid_types = ['INVALID', 'RANDOM', 'NOT_REDDIT_TYPE']

        for content_type in invalid_types:
            with self.subTest(content_type=content_type):
                result = self.handler.create_reddit_file_path(
                    self.base_dir,
                    'test_subreddit',
                    content_type,
                    'test_id'
                )
                self.assertFalse(result.is_safe)
                self.assertIn('Invalid content type', str(result.issues))

    def test_malicious_subreddit_names(self):
        """Test handling of malicious subreddit names."""
        malicious_names = [
            '../../../etc/passwd',
            '..\\..\\..\\windows\\system32',
            '/absolute/path/attack',
            'CON',  # Windows reserved
            'subreddit<script>alert("xss")</script>',
            'sub/with/slashes'
        ]

        for name in malicious_names:
            with self.subTest(name=name):
                result = self.handler.create_reddit_file_path(
                    self.base_dir,
                    name,
                    'POST',
                    'test123'
                )
                if result.is_safe:
                    # If considered safe, ensure it stays within base directory
                    self.assert_path_within_base(result.safe_path)
                    # And doesn't contain the original malicious content
                    self.assertNotIn('../', result.safe_path)
                    self.assertNotIn('..\\', result.safe_path)

    def test_path_length_limits(self):
        """Test path length validation."""
        # Test very long component
        long_component = 'a' * 300  # Longer than typical filesystem limits
        result = self.handler.sanitize_path_component(long_component)
        self.assertTrue(result.is_safe)  # Should be truncated
        self.assertLessEqual(len(result.sanitized_component), 255)

        # Test very long total path
        many_components = ['component'] * 100  # Will create very long path
        result = self.handler.create_safe_path(self.base_dir, *many_components)
        # Should either be safe with reasonable length or rejected
        if result.is_safe:
            self.assertLessEqual(len(result.safe_path), 4096)
        else:
            self.assertIn('too long', str(result.issues))

    def test_empty_and_invalid_inputs(self):
        """Test handling of empty and invalid inputs."""
        invalid_components = ['', None, '   ', '.', '..']

        for component in invalid_components:
            with self.subTest(component=component):
                if component is None:
                    result = self.handler.sanitize_path_component(component)
                    self.assertFalse(result.is_safe)
                else:
                    result = self.handler.sanitize_path_component(component)
                    # Empty or whitespace-only components should be rejected
                    if not component or not component.strip():
                        self.assertFalse(result.is_safe)

    def test_hidden_file_handling(self):
        """Test handling of hidden files (starting with dot)."""
        hidden_files = ['.hidden', '.ssh', '.bashrc']

        for filename in hidden_files:
            with self.subTest(filename=filename):
                result = self.handler.sanitize_path_component(filename)
                self.assertTrue(result.is_safe)
                # Should be prefixed to avoid hidden files
                self.assertTrue(result.sanitized_component.startswith('dot_'))

    def test_unicode_filename_handling(self):
        """Test handling of Unicode filenames."""
        unicode_names = [
            'файл.txt',  # Cyrillic
            '文件.txt',   # Chinese
            'ファイル.txt', # Japanese
            'emoji_😀.txt'  # Emoji
        ]

        for name in unicode_names:
            with self.subTest(name=name):
                result = self.handler.sanitize_path_component(name)
                # Should handle gracefully (either accept or sanitize)
                self.assertIsInstance(result, PathValidationResult)
                if result.is_safe:
                    self.assertIsNotNone(result.sanitized_component)

    def test_case_sensitivity_handling(self):
        """Test case sensitivity in path handling."""
        # Test that paths are handled consistently regardless of case
        components = ['TestDir', 'TestFile.txt']
        result1 = self.handler.create_safe_path(self.base_dir, *components)

        components_lower = ['testdir', 'testfile.txt']
        result2 = self.handler.create_safe_path(self.base_dir, *components_lower)

        self.assertTrue(result1.is_safe)
        self.assertTrue(result2.is_safe)
        # Both should be valid but may have different paths (case preserved)

    def test_path_normalization(self):
        """Test path normalization and resolution."""
        # Paths with redundant separators and current directory references
        redundant_components = ['dir1', '.', 'dir2', 'file.txt']
        result = self.handler.create_safe_path(self.base_dir, *redundant_components)

        self.assertTrue(result.is_safe)
        # Should be normalized (no redundant separators or . references)
        self.assertNotIn('/./', result.safe_path)
        self.assertNotIn('\\.\\', result.safe_path)

    def test_symlink_escape_is_rejected(self):
        """Test rejection of a symlink that resolves outside the base directory."""
        outside_dir = tempfile.mkdtemp()
        self.addCleanup(__import__('shutil').rmtree, outside_dir)
        target = os.path.join(outside_dir, 'outside.txt')
        link = os.path.join(self.base_dir, 'outside_link')

        with open(target, 'w', encoding='utf-8'):
            pass

        try:
            os.symlink(target, link)
        except (AttributeError, NotImplementedError, OSError):
            self.skipTest('symlinks are unavailable')

        self.assertFalse(self.handler.create_safe_path(self.base_dir, 'outside_link').is_safe)
        self.assertFalse(self.handler.validate_existing_path(link, self.base_dir).is_safe)


class TestGlobalPathSecurityFunctions(unittest.TestCase):
    """Test cases for global path security convenience functions."""

    def setUp(self):
        """Set up test fixtures."""
        self.temp_dir = tempfile.mkdtemp()
        self.base_dir = os.path.realpath(self.temp_dir)

    def tearDown(self):
        """Clean up test fixtures."""
        import shutil
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir)

    def test_create_safe_path_function(self):
        """Test the global create_safe_path function."""
        result = create_safe_path(self.base_dir, 'test_file.txt')
        self.assertIsInstance(result, PathValidationResult)
        self.assertTrue(result.is_safe)

    def test_create_reddit_file_path_function(self):
        """Test the global create_reddit_file_path function."""
        result = create_reddit_file_path(
            self.base_dir, 'python', 'POST', 'abc123'
        )
        self.assertIsInstance(result, PathValidationResult)
        self.assertTrue(result.is_safe)
        self.assertIn('POST_abc123.md', result.safe_path)


class TestPathValidationResult(unittest.TestCase):
    """Test cases for PathValidationResult data structure."""

    def test_validation_result_creation(self):
        """Test PathValidationResult creation and default values."""
        result = PathValidationResult(is_safe=True)
        self.assertTrue(result.is_safe)
        self.assertIsNone(result.safe_path)
        self.assertEqual(result.issues, [])
        self.assertIsNone(result.sanitized_component)

    def test_validation_result_with_data(self):
        """Test PathValidationResult with full data."""
        issues = ["Test issue 1", "Test issue 2"]
        result = PathValidationResult(
            is_safe=True,
            safe_path="/safe/path/file.txt",
            issues=issues,
            sanitized_component="sanitized_name.txt"
        )
        self.assertTrue(result.is_safe)
        self.assertEqual(result.safe_path, "/safe/path/file.txt")
        self.assertEqual(result.issues, issues)
        self.assertEqual(result.sanitized_component, "sanitized_name.txt")


if __name__ == '__main__':
    unittest.main()
