"""Circuit breaker states: when calls stop, and when they start again."""

import unittest
from unittest.mock import patch

from utils.media_services.error_isolation import CircuitBreaker, CircuitBreakerConfig, CircuitState


def _boom():
    raise RuntimeError('service down')


class TestCircuitBreaker(unittest.TestCase):
    def setUp(self):
        clock = patch('utils.media_services.error_isolation.time.time', return_value=1000.0)
        self.now = clock.start()
        self.addCleanup(clock.stop)
        self.breaker = CircuitBreaker(
            'svc', CircuitBreakerConfig(failure_threshold=2, recovery_timeout=60, success_threshold=2)
        )

    def _open(self):
        self.breaker.call(_boom)
        self.breaker.call(_boom)
        self.assertEqual(self.breaker.stats.state, CircuitState.OPEN)

    def test_opens_at_the_threshold_and_stops_calling(self):
        self.breaker.call(_boom)
        self.assertFalse(self.breaker.is_rejecting())

        self._open()

        self.assertTrue(self.breaker.is_rejecting())
        calls = []
        self.assertIsNone(self.breaker.call(lambda: calls.append(1)))
        self.assertEqual(calls, [])

    def test_late_success_does_not_close_an_open_breaker(self):
        def succeeds_after_another_call_opens_the_breaker():
            self.now.return_value = 1001.0
            self._open()
            return 'ok'

        self.assertEqual(self.breaker.call(succeeds_after_another_call_opens_the_breaker), 'ok')

        self.assertEqual(self.breaker.stats.state, CircuitState.OPEN)

    def test_stale_success_does_not_count_towards_half_open_recovery(self):
        def succeeds_after_the_breaker_reopens_and_starts_recovering():
            self._open()
            self.now.return_value += 60
            self.breaker.call(lambda: 'ok')
            return 'ok'

        self.breaker.call(succeeds_after_the_breaker_reopens_and_starts_recovering)

        self.assertEqual(self.breaker.stats.state, CircuitState.HALF_OPEN)
        self.assertEqual(self.breaker.stats.success_count, 1)

    def test_success_resets_the_failure_count(self):
        self.breaker.call(_boom)
        self.breaker.call(lambda: 'ok')
        self.breaker.call(_boom)

        self.assertEqual(self.breaker.stats.state, CircuitState.CLOSED)

    def test_returned_failure_counts_but_the_value_is_still_returned(self):
        results = [self.breaker.call(lambda: 'bad', is_failure=lambda value: value == 'bad') for _ in range(2)]

        self.assertEqual(results, ['bad', 'bad'])
        self.assertEqual(self.breaker.stats.state, CircuitState.OPEN)

    def test_recovery_needs_the_timeout_and_enough_successes(self):
        self._open()
        self.now.return_value += 59
        self.assertTrue(self.breaker.is_rejecting())

        self.now.return_value += 1
        self.assertEqual(self.breaker.call(lambda: 'ok'), 'ok')
        self.assertEqual(self.breaker.stats.state, CircuitState.HALF_OPEN)

        self.breaker.call(lambda: 'ok')
        self.assertEqual(self.breaker.stats.state, CircuitState.CLOSED)

    def test_failure_while_half_open_reopens_for_a_full_timeout(self):
        self._open()
        self.now.return_value += 60

        self.breaker.call(_boom)

        self.assertEqual(self.breaker.stats.state, CircuitState.OPEN)
        self.assertTrue(self.breaker.is_rejecting())


if __name__ == '__main__':
    unittest.main()
