#!/usr/bin/env python3
# ruff: noqa: E402
"""
Test runner for Content Recovery System.

This script runs all recovery system tests with proper configuration,
environment setup, and comprehensive reporting.
"""

import os
import sys

# Add project root to path (must be before other imports)
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

import time
import argparse
import unittest
from io import StringIO
from typing import List, Dict, Any

from tests.test_content_recovery import (
    TestWaybackMachineProvider,
    TestPullPushProvider,
    TestRedditPreviewProvider,
    TestRevedditProvider,
    TestRecoveryCacheManager,
    TestContentRecoveryService
)
from tests.test_recovery_integration import (
    TestRecoveryIntegration,
    TestRecoveryPerformance
)


class RecoveryTestRunner:
    """Custom test runner with enhanced reporting for recovery system tests."""

    def __init__(self, verbosity: int = 2):
        self.verbosity = verbosity
        self.results = {}

    def run_test_suite(self, test_classes: List[type], suite_name: str) -> unittest.TestResult:
        """Run a test suite and collect results."""
        print(f"\n🧪 Running {suite_name}")
        print("=" * 60)

        # Create test suite
        loader = unittest.TestLoader()
        suite = unittest.TestSuite()

        for test_class in test_classes:
            tests = loader.loadTestsFromTestCase(test_class)
            suite.addTests(tests)

        # Capture output
        stream = StringIO()
        runner = unittest.TextTestRunner(
            stream=stream,
            verbosity=self.verbosity,
            buffer=True
        )

        # Run tests
        start_time = time.time()
        result = runner.run(suite)
        duration = time.time() - start_time

        # Store results
        self.results[suite_name] = {
            'result': result,
            'duration': duration,
            'output': stream.getvalue()
        }

        # Print summary
        print(f"\n📊 {suite_name} Summary:")
        print(f"   Tests run: {result.testsRun}")
        print(f"   Duration: {duration:.2f}s")
        print(f"   Failures: {len(result.failures)}")
        print(f"   Errors: {len(result.errors)}")
        print(f"   Success Rate: {self._calculate_success_rate(result):.1f}%")

        # Print failures and errors
        if result.failures:
            print(f"\n❌ Failures in {suite_name}:")
            for test, traceback in result.failures:
                print(f"   • {test}")

        if result.errors:
            print(f"\n💥 Errors in {suite_name}:")
            for test, traceback in result.errors:
                print(f"   • {test}")

        return result

    def _calculate_success_rate(self, result: unittest.TestResult) -> float:
        """Calculate success rate percentage."""
        if result.testsRun == 0:
            return 0.0
        failed = len(result.failures) + len(result.errors)
        success = result.testsRun - failed
        return (success / result.testsRun) * 100

    def run_all_tests(self, include_integration: bool = True, include_performance: bool = True) -> Dict[str, Any]:
        """Run all recovery system tests."""
        print("🚀 Content Recovery System Test Suite")
        print("=" * 60)
        print(f"⏰ Started at: {time.strftime('%Y-%m-%d %H:%M:%S')}")

        # Unit tests
        unit_test_classes = [
            TestWaybackMachineProvider,
            TestPullPushProvider,
            TestRedditPreviewProvider,
            TestRevedditProvider,
            TestRecoveryCacheManager,
            TestContentRecoveryService,
        ]

        self.run_test_suite(unit_test_classes, "Unit Tests")

        # Integration tests
        if include_integration:
            integration_test_classes = [TestRecoveryIntegration]
            self.run_test_suite(integration_test_classes, "Integration Tests")

        # Performance tests
        if include_performance:
            performance_test_classes = [TestRecoveryPerformance]
            self.run_test_suite(performance_test_classes, "Performance Tests")

        # Generate final report
        return self._generate_final_report()

    def _generate_final_report(self) -> Dict[str, Any]:
        """Generate comprehensive test report."""
        print("\n" + "=" * 60)
        print("🏁 FINAL TEST REPORT")
        print("=" * 60)

        total_tests = 0
        total_failures = 0
        total_errors = 0
        total_duration = 0

        for suite_name, data in self.results.items():
            result = data['result']
            duration = data['duration']

            total_tests += result.testsRun
            total_failures += len(result.failures)
            total_errors += len(result.errors)
            total_duration += duration

            success_rate = self._calculate_success_rate(result)
            status = "✅" if success_rate == 100 else "⚠️" if success_rate >= 80 else "❌"

            print(f"{status} {suite_name}: {success_rate:.1f}% ({result.testsRun} tests, {duration:.1f}s)")

        overall_success_rate = ((total_tests - total_failures - total_errors) / total_tests * 100) if total_tests > 0 else 0

        print("\n📈 Overall Statistics:")
        print(f"   Total Tests: {total_tests}")
        print(f"   Total Duration: {total_duration:.1f}s")
        print(f"   Success Rate: {overall_success_rate:.1f}%")
        print(f"   Failures: {total_failures}")
        print(f"   Errors: {total_errors}")

        # Generate recommendations
        self._generate_recommendations()

        return {
            'total_tests': total_tests,
            'total_failures': total_failures,
            'total_errors': total_errors,
            'success_rate': overall_success_rate,
            'duration': total_duration,
            'results': self.results
        }

    def _generate_recommendations(self):
        """Generate recommendations based on test results."""
        print("\n💡 Recommendations:")

        # Check Wayback Machine success rate
        unit_result = self.results.get("Unit Tests", {}).get('result')
        if unit_result:
            wayback_failures = [f for f, _ in unit_result.failures if 'Wayback' in str(f)]
            if wayback_failures:
                print("   • Wayback Machine tests failing - check network connectivity")

        # Check PullPush.io issues
        pullpush_errors = []
        for suite_data in self.results.values():
            result = suite_data['result']
            pullpush_errors.extend([e for e, _ in result.errors if 'PullPush' in str(e)])

        if pullpush_errors:
            print("   • PullPush.io errors detected - service may be rate limited or down")

        # Check cache issues
        cache_failures = []
        for suite_data in self.results.values():
            result = suite_data['result']
            cache_failures.extend([f for f, _ in result.failures if 'Cache' in str(f)])

        if cache_failures:
            print("   • Cache system issues - check SQLite permissions and disk space")

        # Performance recommendations
        if "Performance Tests" in self.results:
            perf_result = self.results["Performance Tests"]['result']
            if len(perf_result.failures) > 0:
                print("   • Performance issues detected - consider adjusting timeouts")

        print("   • Run tests with -v flag for detailed output")
        print("   • Check logs for rate limiting warnings")
        print("   • Verify internet connectivity for external API tests")


def main():
    """Main test runner entry point."""
    parser = argparse.ArgumentParser(description="Content Recovery System Test Runner")
    parser.add_argument('-v', '--verbose', action='store_true',
                       help="Verbose output")
    parser.add_argument('--unit-only', action='store_true',
                       help="Run only unit tests")
    parser.add_argument('--no-integration', action='store_true',
                       help="Skip integration tests")
    parser.add_argument('--no-performance', action='store_true',
                       help="Skip performance tests")
    parser.add_argument('--provider', choices=['wayback', 'pullpush', 'reddit_preview', 'reveddit'],
                       help="Test only specific provider")

    args = parser.parse_args()

    # Configure verbosity
    verbosity = 2 if args.verbose else 1

    # Create test runner
    runner = RecoveryTestRunner(verbosity=verbosity)

    # Determine which tests to run
    include_integration = not args.no_integration and not args.unit_only
    include_performance = not args.no_performance and not args.unit_only

    if args.provider:
        # Run only specific provider tests
        provider_map = {
            'wayback': [TestWaybackMachineProvider],
            'pullpush': [TestPullPushProvider],
            'reddit_preview': [TestRedditPreviewProvider],
            'reveddit': [TestRevedditProvider]
        }

        test_classes = provider_map.get(args.provider, [])
        if test_classes:
            result = runner.run_test_suite(test_classes, f"{args.provider.title()} Provider Tests")
            success = result.wasSuccessful()
        else:
            print(f"❌ Unknown provider: {args.provider}")
            return 1
    else:
        # Run all tests
        report = runner.run_all_tests(
            include_integration=include_integration,
            include_performance=include_performance
        )
        success = report['success_rate'] == 100.0

    # Exit with appropriate code
    return 0 if success else 1


if __name__ == '__main__':
    exit_code = main()
    sys.exit(exit_code)