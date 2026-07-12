"""
Unit tests for disk space validation functionality.

Tests disk space checking, safety factor calculations,
and download validation based on available storage.
"""

import unittest
import tempfile
import os
import sys
from unittest.mock import patch, MagicMock
from collections import namedtuple

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.media_services.base_downloader import BaseHTTPDownloader
from utils.service_abstractions import ServiceConfig
from utils.constants import DISK_SPACE_SAFETY_FACTOR


# Mock DiskUsage object to simulate shutil.disk_usage return value
DiskUsage = namedtuple('DiskUsage', ['total', 'used', 'free'])


def create_downloader():
    return BaseHTTPDownloader(ServiceConfig(name='disk-space-test'))


class TestDiskSpaceValidation(unittest.TestCase):
    """Test cases for disk space validation functionality."""

    def setUp(self):
        """Set up test fixtures."""
        self.downloader = create_downloader()
        self.temp_dir = tempfile.mkdtemp()

    def tearDown(self):
        """Clean up test fixtures."""
        import shutil
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir)

    def test_disk_space_check_sufficient_space(self):
        """Test disk space check when sufficient space is available."""
        file_size = 100 * 1024 * 1024  # 100MB
        save_path = os.path.join(self.temp_dir, 'test_file.txt')

        # Mock shutil.disk_usage to return sufficient free space
        required_space = int(file_size * DISK_SPACE_SAFETY_FACTOR)
        available_space = required_space * 2  # Double the required space

        mock_usage = DiskUsage(
            total=1000 * 1024 * 1024 * 1024,  # 1TB total
            used=500 * 1024 * 1024 * 1024,    # 500GB used
            free=available_space               # Sufficient free space
        )

        with patch('shutil.disk_usage', return_value=mock_usage):
            result = self.downloader._check_disk_space(file_size, save_path)
            self.assertTrue(result)

    def test_disk_space_check_insufficient_space(self):
        """Test disk space check when insufficient space is available."""
        file_size = 100 * 1024 * 1024  # 100MB
        save_path = os.path.join(self.temp_dir, 'test_file.txt')

        # Mock shutil.disk_usage to return insufficient free space
        required_space = int(file_size * DISK_SPACE_SAFETY_FACTOR)
        available_space = required_space // 2  # Half the required space

        mock_usage = DiskUsage(
            total=1000 * 1024 * 1024 * 1024,  # 1TB total
            used=900 * 1024 * 1024 * 1024,    # 900GB used
            free=available_space               # Insufficient free space
        )

        with patch('shutil.disk_usage', return_value=mock_usage):
            result = self.downloader._check_disk_space(file_size, save_path)
            self.assertFalse(result)

    def test_disk_space_check_zero_file_size(self):
        """Test disk space check with zero file size."""
        file_size = 0
        save_path = os.path.join(self.temp_dir, 'test_file.txt')

        # Should return True for unknown/zero file size
        result = self.downloader._check_disk_space(file_size, save_path)
        self.assertTrue(result)

    def test_disk_space_check_negative_file_size(self):
        """Test disk space check with negative file size."""
        file_size = -100
        save_path = os.path.join(self.temp_dir, 'test_file.txt')

        # Should return True for invalid file size
        result = self.downloader._check_disk_space(file_size, save_path)
        self.assertTrue(result)

    def test_disk_space_check_safety_factor_calculation(self):
        """Test that safety factor is correctly applied."""
        file_size = 100 * 1024 * 1024  # 100MB
        save_path = os.path.join(self.temp_dir, 'test_file.txt')

        # Calculate expected required space with safety factor
        expected_required = int(file_size * DISK_SPACE_SAFETY_FACTOR)

        # Set available space to exactly the required amount
        mock_usage = DiskUsage(
            total=1000 * 1024 * 1024 * 1024,
            used=500 * 1024 * 1024 * 1024,
            free=expected_required
        )

        with patch('shutil.disk_usage', return_value=mock_usage):
            result = self.downloader._check_disk_space(file_size, save_path)
            self.assertTrue(result)  # Should have exactly enough space

        # Test with slightly less space
        mock_usage_insufficient = DiskUsage(
            total=1000 * 1024 * 1024 * 1024,
            used=500 * 1024 * 1024 * 1024,
            free=expected_required - 1  # 1 byte less than required
        )

        with patch('shutil.disk_usage', return_value=mock_usage_insufficient):
            result = self.downloader._check_disk_space(file_size, save_path)
            self.assertFalse(result)  # Should not have enough space

    def test_disk_space_check_large_files(self):
        """Test disk space check with very large files."""
        file_size = 10 * 1024 * 1024 * 1024  # 10GB
        save_path = os.path.join(self.temp_dir, 'large_file.txt')

        required_space = int(file_size * DISK_SPACE_SAFETY_FACTOR)

        # Test with sufficient space for large file
        mock_usage_sufficient = DiskUsage(
            total=100 * 1024 * 1024 * 1024 * 1024,  # 100TB
            used=50 * 1024 * 1024 * 1024 * 1024,    # 50TB
            free=required_space * 2                  # Double required space
        )

        with patch('shutil.disk_usage', return_value=mock_usage_sufficient):
            result = self.downloader._check_disk_space(file_size, save_path)
            self.assertTrue(result)

        # Test with insufficient space for large file
        mock_usage_insufficient = DiskUsage(
            total=20 * 1024 * 1024 * 1024,  # 20GB
            used=15 * 1024 * 1024 * 1024,   # 15GB
            free=4 * 1024 * 1024 * 1024     # 4GB (insufficient)
        )

        with patch('shutil.disk_usage', return_value=mock_usage_insufficient):
            result = self.downloader._check_disk_space(file_size, save_path)
            self.assertFalse(result)

    def test_disk_space_check_directory_handling(self):
        """Test disk space check with different directory scenarios."""
        file_size = 50 * 1024 * 1024  # 50MB

        test_cases = [
            # Test with full path
            (os.path.join(self.temp_dir, 'subdir', 'file.txt'), os.path.join(self.temp_dir, 'subdir')),
            # Test with filename only (should use current directory)
            ('file.txt', '.'),
            # Test with relative path
            (os.path.join('relative', 'path', 'file.txt'), os.path.join('relative', 'path')),
        ]

        mock_usage = DiskUsage(
            total=1000 * 1024 * 1024 * 1024,
            used=500 * 1024 * 1024 * 1024,
            free=200 * 1024 * 1024 * 1024  # 200GB free
        )

        for save_path, expected_directory in test_cases:
            with self.subTest(save_path=save_path):
                with patch('shutil.disk_usage', return_value=mock_usage) as mock_disk_usage:
                    result = self.downloader._check_disk_space(file_size, save_path)
                    self.assertTrue(result)
                    mock_disk_usage.assert_called_once_with(expected_directory)

    def test_disk_space_check_os_error_handling(self):
        """Test disk space check when OS errors occur."""
        file_size = 100 * 1024 * 1024  # 100MB
        save_path = os.path.join(self.temp_dir, 'test_file.txt')

        os_errors = [
            OSError("Disk not accessible"),
            FileNotFoundError("Directory not found"),
            PermissionError("Permission denied"),
            ValueError("Invalid path"),
        ]

        for error in os_errors:
            with self.subTest(error=error):
                with patch('shutil.disk_usage', side_effect=error):
                    # Should return True (allow download) when disk check fails
                    result = self.downloader._check_disk_space(file_size, save_path)
                    self.assertTrue(result)

    def test_disk_space_check_edge_cases(self):
        """Test disk space check with edge cases."""
        save_path = os.path.join(self.temp_dir, 'test_file.txt')

        edge_cases = [
            {
                'file_size': 1,  # Minimum file size
                'description': 'Minimum file size',
                'expected': True,
            },
            {
                'file_size': 2**63 - 1,  # Maximum int64
                'description': 'Maximum file size',
                'expected': False,
            },
            {
                'file_size': 1024,  # Small file
                'description': 'Small file size',
                'expected': True,
            }
        ]

        # Use a large amount of free space to ensure edge cases pass
        mock_usage = DiskUsage(
            total=1000 * 1024 * 1024 * 1024 * 1024,  # 1000TB
            used=100 * 1024 * 1024 * 1024 * 1024,    # 100TB
            free=900 * 1024 * 1024 * 1024 * 1024     # 900TB
        )

        for case in edge_cases:
            with self.subTest(case=case['description']):
                with patch('shutil.disk_usage', return_value=mock_usage):
                    result = self.downloader._check_disk_space(case['file_size'], save_path)
                    self.assertEqual(result, case['expected'])

    def test_disk_space_logging(self):
        """Test that appropriate log messages are generated."""
        file_size = 100 * 1024 * 1024  # 100MB
        save_path = os.path.join(self.temp_dir, 'test_file.txt')

        # Test logging for insufficient space
        insufficient_space = 10 * 1024 * 1024  # 10MB (insufficient)
        mock_usage_insufficient = DiskUsage(
            total=1000 * 1024 * 1024 * 1024,
            used=990 * 1024 * 1024 * 1024,
            free=insufficient_space
        )

        with patch('shutil.disk_usage', return_value=mock_usage_insufficient):
            with patch.object(self.downloader._logger, 'warning') as mock_warning:
                result = self.downloader._check_disk_space(file_size, save_path)
                self.assertFalse(result)
                mock_warning.assert_called_once()
                # Check that warning message contains expected content
                warning_call_args = mock_warning.call_args[0][0]
                self.assertIn('Insufficient disk space', warning_call_args)

        # Test logging for sufficient space (debug level)
        sufficient_space = 1024 * 1024 * 1024  # 1GB (sufficient)
        mock_usage_sufficient = DiskUsage(
            total=1000 * 1024 * 1024 * 1024,
            used=500 * 1024 * 1024 * 1024,
            free=sufficient_space
        )

        with patch('shutil.disk_usage', return_value=mock_usage_sufficient):
            with patch.object(self.downloader._logger, 'debug') as mock_debug:
                result = self.downloader._check_disk_space(file_size, save_path)
                self.assertTrue(result)
                mock_debug.assert_called_once()
                # Check that debug message contains expected content
                debug_call_args = mock_debug.call_args[0][0]
                self.assertIn('Disk space check passed', debug_call_args)

    def test_disk_space_units_conversion(self):
        """Test proper conversion of disk space units in logging."""
        file_size = 1024 * 1024 * 1024  # 1GB
        save_path = os.path.join(self.temp_dir, 'test_file.txt')

        # Set up specific values to test unit conversion
        mock_usage = DiskUsage(
            total=10 * 1024**3,  # 10GB
            used=5 * 1024**3,    # 5GB
            free=5 * 1024**3     # 5GB
        )

        with patch('shutil.disk_usage', return_value=mock_usage):
            with patch.object(self.downloader._logger, 'debug') as mock_debug:
                result = self.downloader._check_disk_space(file_size, save_path)
                self.assertTrue(result)

                # Verify that logging includes properly formatted units
                debug_call_args = mock_debug.call_args[0][0]
                # Should include MB for required space and GB for available space
                self.assertIn('MB', debug_call_args)  # Required space in MB
                self.assertIn('GB', debug_call_args)  # Available space in GB

    def test_integration_with_download_file_method(self):
        """Test integration of disk space check with download_file method."""
        # This tests that disk space check is properly called during downloads
        file_size = 10 * 1024 * 1024  # 10MB, below the downloader size limit
        url = "https://example.com/test_file.txt"
        save_path = os.path.join(self.temp_dir, 'test_file.txt')

        # Mock insufficient disk space
        mock_usage = DiskUsage(
            total=1000 * 1024 * 1024,
            used=950 * 1024 * 1024,
            free=10 * 1024 * 1024  # Only 10MB free
        )

        with patch('shutil.disk_usage', return_value=mock_usage):
            with patch.object(self.downloader._session, 'get') as mock_get:
                # Mock the streaming response headers before the download begins.
                mock_response = MagicMock()
                mock_response.headers = {'content-length': str(file_size)}
                mock_response.raise_for_status.return_value = None
                mock_response.__enter__.return_value = mock_response
                mock_get.return_value = mock_response

                # Should not proceed with download due to insufficient space
                result = self.downloader.download_file(url, save_path)
                self.assertIsNotNone(result)
                self.assertFalse(result.is_success)
                self.assertIn('disk space', result.error_message.lower())
                mock_response.iter_content.assert_not_called()
                self.assertFalse(os.path.exists(save_path))

    def test_disk_space_safety_factor_is_applied(self):
        """Test that safety-factor multiplication rejects insufficient space."""
        file_size = 100 * 1024 * 1024  # 100MB
        save_path = os.path.join(self.temp_dir, 'test_file.txt')

        # Set free space to exactly file_size (without safety factor)
        mock_usage = DiskUsage(
            total=1000 * 1024 * 1024 * 1024,
            used=900 * 1024 * 1024 * 1024,
            free=file_size  # Exactly the file size
        )

        with patch('shutil.disk_usage', return_value=mock_usage):
            # Should fail because safety factor requires more space
            result = self.downloader._check_disk_space(file_size, save_path)
            self.assertFalse(result)


class TestDiskSpaceValidationHelpers(unittest.TestCase):
    """Test helper methods and edge cases for disk space validation."""

    def test_disk_usage_cross_platform(self):
        """Test that the downloader handles a standard disk-usage result."""
        usage = DiskUsage(total=1000, used=100, free=900)

        with patch('shutil.disk_usage', return_value=usage) as mock_disk_usage:
            self.assertTrue(create_downloader()._check_disk_space(1, 'file.txt'))
            mock_disk_usage.assert_called_once_with('.')

    def test_safety_factor_edge_cases(self):
        """Test safety factor calculations with edge cases."""
        downloader = create_downloader()

        # Test with very small files
        small_file = 1024  # 1KB
        temp_dir = tempfile.mkdtemp()
        save_path = os.path.join(temp_dir, 'small_file.txt')

        try:
            # Large amount of free space
            mock_usage = DiskUsage(
                total=1000 * 1024**3,
                used=100 * 1024**3,
                free=900 * 1024**3
            )

            with patch('shutil.disk_usage', return_value=mock_usage):
                result = downloader._check_disk_space(small_file, save_path)
                self.assertTrue(result)

        finally:
            import shutil
            if os.path.exists(temp_dir):
                shutil.rmtree(temp_dir)

    def test_concurrent_disk_space_checks(self):
        """Test disk space checking under concurrent scenarios."""
        import threading

        downloader = create_downloader()
        file_size = 50 * 1024 * 1024  # 50MB
        temp_dir = tempfile.mkdtemp()
        usage = DiskUsage(total=1000 * 1024**3, used=100 * 1024**3, free=900 * 1024**3)

        try:
            results = []
            errors = []

            def check_space():
                try:
                    save_path = os.path.join(temp_dir, f'file_{threading.current_thread().ident}.txt')
                    result = downloader._check_disk_space(file_size, save_path)
                    results.append(result)
                except Exception as e:
                    errors.append(e)

            with patch('shutil.disk_usage', return_value=usage) as mock_disk_usage:
                threads = [threading.Thread(target=check_space) for _ in range(5)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(timeout=5)

            self.assertEqual(mock_disk_usage.call_count, 5)
            self.assertEqual(len(errors), 0, f"Errors occurred: {errors}")
            self.assertEqual(len(results), 5)
            for result in results:
                self.assertIsInstance(result, bool)

        finally:
            import shutil
            if os.path.exists(temp_dir):
                shutil.rmtree(temp_dir)


if __name__ == '__main__':
    unittest.main()
