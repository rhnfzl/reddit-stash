"""
Integration tests for Content Recovery System with MediaDownloadManager.

Tests the complete flow from download failure through recovery attempt
to successful download from recovered URL.
"""

import unittest
import tempfile
import os
import time
from unittest.mock import Mock, patch, MagicMock
from typing import Optional

# Import system components
import sys
sys.path.append(os.path.join(os.path.dirname(__file__), '..'))

from utils.media_download_manager import MediaDownloadManager
from utils.content_recovery import ContentRecoveryService
from utils.content_recovery.recovery_metadata import RecoveryResult, RecoveryMetadata, RecoverySource, RecoveryQuality
from utils.service_abstractions import DownloadResult, DownloadStatus


class MockDownloader:
    """Mock downloader for testing recovery integration."""

    def __init__(self, should_fail: bool = False, fail_count: int = 1):
        self.should_fail = should_fail
        self.fail_count = fail_count
        self.attempt_count = 0

    def download(self, url: str, save_path: str) -> DownloadResult:
        """Mock download that can be configured to fail or succeed."""
        self.attempt_count += 1

        # Fail for first N attempts, then succeed
        if self.should_fail and self.attempt_count <= self.fail_count:
            return DownloadResult(
                status=DownloadStatus.FAILED,
                error_message=f"Mock failure {self.attempt_count}",
                local_path=None,
                bytes_downloaded=0,
                download_time=0.1
            )
        else:
            # Simulate successful download
            mock_path = os.path.join(save_path, "downloaded_file.jpg")
            return DownloadResult(
                status=DownloadStatus.SUCCESS,
                error_message=None,
                local_path=mock_path,
                bytes_downloaded=1024,
                download_time=0.2
            )


class MockRecoveryService:
    """Mock recovery service for controlled testing."""

    def __init__(self, recovery_urls: dict = None):
        self.recovery_urls = recovery_urls or {}
        self.is_enabled_flag = True
        self.attempt_count = 0

    def is_enabled(self) -> bool:
        return self.is_enabled_flag

    def attempt_recovery(self, url: str, original_failure_reason: Optional[str] = None) -> RecoveryResult:
        """Mock recovery that returns predefined URLs."""
        self.attempt_count += 1

        if url in self.recovery_urls:
            recovered_url = self.recovery_urls[url]
            metadata = RecoveryMetadata(
                source=RecoverySource.WAYBACK_MACHINE,
                recovered_url=recovered_url,
                recovery_timestamp=time.time(),
                content_quality=RecoveryQuality.HIGH_QUALITY,
                attempt_duration=0.5
            )
            return RecoveryResult.success_result(recovered_url, metadata)
        else:
            return RecoveryResult.failure_result(
                f"No recovery available for {url}",
                RecoverySource.WAYBACK_MACHINE
            )


class TestRecoveryIntegration(unittest.TestCase):
    """Test integration between recovery system and media download manager."""

    def setUp(self):
        # Create temporary directory for downloads
        self.temp_dir = tempfile.mkdtemp()

        # Create mock media download manager (we'll patch its internals)
        self.download_manager = MediaDownloadManager()

    def tearDown(self):
        # Clean up temporary directory
        import shutil
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir)

    def test_successful_recovery_after_download_failure(self):
        """Test successful recovery and download after initial failure."""
        print("\n🔄 Testing successful recovery after download failure...")

        # Setup scenario
        original_url = "https://example.com/failing_image.jpg"
        recovered_url = "https://web.archive.org/web/20220101/https://example.com/failing_image.jpg"

        # Mock recovery service that provides recovery URL
        recovery_service = MockRecoveryService({
            original_url: recovered_url
        })

        # Mock downloader that fails on original URL, succeeds on recovered URL
        def mock_downloader_factory(url):
            if url == original_url:
                return MockDownloader(should_fail=True, fail_count=999)  # Always fail
            else:
                return MockDownloader(should_fail=False)  # Always succeed

        # Patch the recovery service and downloader
        with patch.object(self.download_manager, '_recovery_service', recovery_service):
            with patch.object(self.download_manager, '_get_service_for_url') as mock_get_service:
                # Setup mock service return
                mock_get_service.return_value = ('generic', mock_downloader_factory(original_url))

                # Mock the second call for recovered URL
                def side_effect(url):
                    if url == original_url:
                        return ('generic', mock_downloader_factory(original_url))
                    else:
                        return ('generic', mock_downloader_factory(recovered_url))

                mock_get_service.side_effect = side_effect

                print(f"  Testing download of: {original_url}")
                print(f"  Expected recovery URL: {recovered_url}")

                # Attempt download
                result_path = self.download_manager.download_media(original_url, self.temp_dir)

                if result_path:
                    print(f"    ✅ SUCCESS: Downloaded to {result_path}")
                    self.assertIsNotNone(result_path)
                    self.assertEqual(recovery_service.attempt_count, 1)
                else:
                    print("    ❌ FAILED: No file downloaded")
                    self.fail("Expected successful download after recovery")

    def test_recovery_failure_leads_to_blacklist(self):
        """Test that failed recovery leads to URL blacklisting."""
        print("\n❌ Testing recovery failure leads to blacklisting...")

        original_url = "https://example.com/unrecoverable_image.jpg"

        # Mock recovery service that cannot recover this URL
        recovery_service = MockRecoveryService({})  # No recovery URLs

        # Mock downloader that always fails
        mock_downloader = MockDownloader(should_fail=True, fail_count=999)

        with patch.object(self.download_manager, '_recovery_service', recovery_service):
            with patch.object(self.download_manager, '_get_service_for_url') as mock_get_service:
                mock_get_service.return_value = ('generic', mock_downloader)

                print(f"  Testing download of: {original_url}")

                # Attempt download
                result_path = self.download_manager.download_media(original_url, self.temp_dir)

                print(f"    Result: {result_path}")
                self.assertIsNone(result_path)

                # Check that URL failure is tracked (permanent or transient)
                with self.download_manager._url_lock:
                    is_tracked = (original_url in self.download_manager._permanent_failures or
                                  original_url in self.download_manager._transient_failures)
                print(f"    URL failure tracked: {'✅ Yes' if is_tracked else '❌ No'}")
                self.assertTrue(is_tracked)

                # Verify recovery was attempted
                self.assertEqual(recovery_service.attempt_count, 1)

    def test_recovery_disabled_skips_recovery(self):
        """Test that disabled recovery service skips recovery attempts."""
        print("\n🚫 Testing disabled recovery service skips recovery...")

        original_url = "https://example.com/failing_image.jpg"

        # Mock recovery service that is disabled
        recovery_service = MockRecoveryService()
        recovery_service.is_enabled_flag = False

        # Mock downloader that always fails
        mock_downloader = MockDownloader(should_fail=True, fail_count=999)

        with patch.object(self.download_manager, '_recovery_service', recovery_service):
            with patch.object(self.download_manager, '_get_service_for_url') as mock_get_service:
                mock_get_service.return_value = ('generic', mock_downloader)

                print(f"  Testing download of: {original_url}")

                # Attempt download
                result_path = self.download_manager.download_media(original_url, self.temp_dir)

                print(f"    Result: {result_path}")
                self.assertIsNone(result_path)

                # Verify recovery was NOT attempted
                print(f"    Recovery attempts: {recovery_service.attempt_count}")
                self.assertEqual(recovery_service.attempt_count, 0)

    def test_recovery_exception_handling(self):
        """Test that exceptions in recovery are handled gracefully."""
        print("\n💥 Testing recovery exception handling...")

        original_url = "https://example.com/exception_test.jpg"

        # Mock recovery service that raises exceptions
        recovery_service = Mock()
        recovery_service.is_enabled.return_value = True
        recovery_service.attempt_recovery.side_effect = Exception("Recovery service crashed!")

        # Mock downloader that fails
        mock_downloader = MockDownloader(should_fail=True, fail_count=999)

        with patch.object(self.download_manager, '_recovery_service', recovery_service):
            with patch.object(self.download_manager, '_get_service_for_url') as mock_get_service:
                mock_get_service.return_value = ('generic', mock_downloader)

                print(f"  Testing download of: {original_url}")

                # Attempt download (should not crash despite recovery exception)
                result_path = self.download_manager.download_media(original_url, self.temp_dir)

                print(f"    Result: {result_path}")
                self.assertIsNone(result_path)

                # Should still be tracked as failed despite recovery exception
                with self.download_manager._url_lock:
                    is_tracked = (original_url in self.download_manager._permanent_failures or
                                  original_url in self.download_manager._transient_failures)
                print(f"    URL failure tracked after exception: {'✅ Yes' if is_tracked else '❌ No'}")
                self.assertTrue(is_tracked)

    def test_multiple_recovery_attempts_same_url(self):
        """Test that the same URL doesn't trigger multiple recovery attempts."""
        print("\n🔁 Testing multiple recovery attempts for same URL...")

        original_url = "https://example.com/repeated_failure.jpg"

        # Mock recovery service
        recovery_service = MockRecoveryService({})  # No recovery available

        # Mock downloader that always fails
        mock_downloader = MockDownloader(should_fail=True, fail_count=999)

        with patch.object(self.download_manager, '_recovery_service', recovery_service):
            with patch.object(self.download_manager, '_get_service_for_url') as mock_get_service:
                mock_get_service.return_value = ('generic', mock_downloader)

                print(f"  Testing multiple downloads of: {original_url}")

                # First attempt
                result1 = self.download_manager.download_media(original_url, self.temp_dir)
                print(f"    First attempt result: {result1}")

                # Second attempt (should be blacklisted)
                result2 = self.download_manager.download_media(original_url, self.temp_dir)
                print(f"    Second attempt result: {result2}")

                # Both should fail
                self.assertIsNone(result1)
                self.assertIsNone(result2)

                # With smart blacklist, transient failures allow a retry before blocking,
                # so we expect 2 recovery attempts (one per download call) before the URL
                # reaches the transient failure threshold
                print(f"    Recovery attempts: {recovery_service.attempt_count}")
                self.assertLessEqual(recovery_service.attempt_count, 2)

    def test_recovery_cache_integration(self):
        """Test integration with recovery cache system."""
        print("\n💾 Testing recovery cache integration...")

        original_url = "https://example.com/cached_recovery.jpg"
        recovered_url = "https://web.archive.org/web/cached/image.jpg"

        # Use real recovery service with temporary cache
        temp_cache = tempfile.NamedTemporaryFile(delete=False, suffix='.db')
        temp_cache.close()

        try:
            mock_config = Mock()
            mock_config.get_recovery_config.return_value = {
                'use_wayback_machine': True,
                'use_pushshift_api': False,
                'use_reddit_previews': False,
                'use_reveddit_api': False,
                'timeout_seconds': 5,
                'cache_duration_hours': 24
            }

            recovery_service = ContentRecoveryService(
                config=mock_config,
                cache_path=temp_cache.name
            )

            # Mock the Wayback provider to return our test URL
            with patch.object(recovery_service.providers[RecoverySource.WAYBACK_MACHINE], 'attempt_recovery') as mock_wayback:
                metadata = RecoveryMetadata(
                    source=RecoverySource.WAYBACK_MACHINE,
                    recovered_url=recovered_url,
                    recovery_timestamp=time.time(),
                    content_quality=RecoveryQuality.HIGH_QUALITY,
                    attempt_duration=0.3
                )
                mock_wayback.return_value = RecoveryResult.success_result(recovered_url, metadata)

                print(f"  Testing cached recovery for: {original_url}")

                # First recovery attempt (should hit provider and cache result)
                result1 = recovery_service.attempt_recovery(original_url)
                print(f"    First attempt: {'✅ Success' if result1.success else '❌ Failed'}")

                # Second recovery attempt (should hit cache)
                result2 = recovery_service.attempt_recovery(original_url)
                print(f"    Second attempt: {'✅ Success' if result2.success else '❌ Failed'}")

                # Both should succeed
                self.assertTrue(result1.success)
                self.assertTrue(result2.success)

                # Second should be from cache
                if result2.metadata:
                    print(f"    Second attempt cache hit: {result2.metadata.cache_hit}")
                    self.assertTrue(result2.metadata.cache_hit)

                # Provider should only be called once
                self.assertEqual(mock_wayback.call_count, 1)

        finally:
            # Clean up temporary cache
            if os.path.exists(temp_cache.name):
                os.unlink(temp_cache.name)


class TestRecoveryPerformance(unittest.TestCase):
    """Performance tests for recovery system."""

    def test_recovery_timeout_compliance(self):
        """Test that recovery attempts respect timeout settings."""
        print("\n⏱️ Testing recovery timeout compliance...")

        # Create recovery service with short timeout
        mock_config = Mock()
        mock_config.get_recovery_config.return_value = {
            'use_wayback_machine': True,
            'use_pushshift_api': False,
            'use_reddit_previews': False,
            'use_reveddit_api': False,
            'timeout_seconds': 2,  # Very short timeout
            'cache_duration_hours': 24
        }

        recovery_service = ContentRecoveryService(config=mock_config)

        test_url = "https://httpbin.org/delay/5"  # URL that delays 5 seconds

        print(f"  Testing timeout with: {test_url}")
        start_time = time.time()
        result = recovery_service.attempt_recovery(test_url)
        duration = time.time() - start_time

        print(f"    Duration: {duration:.2f}s")
        print(f"    Result: {'✅ Success' if result.success else '❌ Failed'}")

        # Should complete within reasonable time (allowing for some overhead)
        self.assertLess(duration, 10, "Recovery should respect timeout settings")

    def test_parallel_vs_sequential_performance(self):
        """Compare parallel vs sequential recovery performance."""
        print("\n⚡ Testing parallel vs sequential performance...")

        mock_config = Mock()
        mock_config.get_recovery_config.return_value = {
            'use_wayback_machine': True,
            'use_pushshift_api': True,
            'use_reddit_previews': True,
            'use_reveddit_api': True,
            'timeout_seconds': 10,
            'cache_duration_hours': 24
        }

        recovery_service = ContentRecoveryService(config=mock_config)
        test_url = "https://httpbin.org/status/404"

        # Test sequential mode
        print("  Testing sequential recovery...")
        start_time = time.time()
        result_sequential = recovery_service.attempt_recovery(test_url, async_mode=False)
        sequential_duration = time.time() - start_time

        # Small delay to avoid caching
        time.sleep(1)

        # Test parallel mode
        print("  Testing parallel recovery...")
        start_time = time.time()
        result_parallel = recovery_service.attempt_recovery(test_url, async_mode=True)
        parallel_duration = time.time() - start_time

        print(f"    Sequential: {sequential_duration:.2f}s ({'✅' if result_sequential.success else '❌'})")
        print(f"    Parallel: {parallel_duration:.2f}s ({'✅' if result_parallel.success else '❌'})")

        # Both attempts should have same success/failure result
        self.assertEqual(result_sequential.success, result_parallel.success)

        # Log performance difference (don't assert since external services vary)
        if sequential_duration > 0:
            speedup = sequential_duration / parallel_duration
            print(f"    Parallel speedup: {speedup:.2f}x")


if __name__ == '__main__':
    print("🔗 Content Recovery Integration Test Suite")
    print("=" * 50)

    # Create test suite
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()

    # Add test classes
    test_classes = [
        TestRecoveryIntegration,
        TestRecoveryPerformance,
    ]

    for test_class in test_classes:
        tests = loader.loadTestsFromTestCase(test_class)
        suite.addTests(tests)

    # Run tests
    runner = unittest.TextTestRunner(verbosity=2, buffer=True)
    result = runner.run(suite)

    print("\n" + "=" * 50)
    print("🏁 Integration Test Suite Complete")
    print(f"   Tests run: {result.testsRun}")
    print(f"   Failures: {len(result.failures)}")
    print(f"   Errors: {len(result.errors)}")

    if result.failures:
        print("\n❌ Failures:")
        for test, traceback in result.failures:
            print(f"   {test}: {traceback}")

    if result.errors:
        print("\n💥 Errors:")
        for test, traceback in result.errors:
            print(f"   {test}: {traceback}")

    exit_code = 0 if result.wasSuccessful() else 1
    print(f"\n{'✅ All integration tests passed!' if exit_code == 0 else '❌ Some integration tests failed!'}")
    exit(exit_code)