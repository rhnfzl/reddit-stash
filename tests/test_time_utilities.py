"""Sleep calculations and backoff."""

import unittest
from unittest.mock import patch

from utils.constants import DYNAMIC_SLEEP_BASE_SECONDS
from utils.time_utilities import dynamic_sleep, exponential_backoff


class TestTimeUtilities(unittest.TestCase):
    def setUp(self):
        for target, name in (('time.sleep', 'sleep'), ('random.uniform', 'uniform')):
            patcher = patch(f'utils.time_utilities.{target}')
            setattr(self, name, patcher.start())
            self.addCleanup(patcher.stop)

    def test_dynamic_sleep_scales_with_length_failures_and_cap(self):
        self.uniform.return_value = 1.0
        base = DYNAMIC_SLEEP_BASE_SECONDS
        for args, expected in (
            ((0,), base),
            ((20000,), base + 0.02),
            ((0, 2), base * 1.5 ** 2),
            ((10 ** 9, 5, 0.25), 0.25),
        ):
            with self.subTest(args=args):
                self.assertAlmostEqual(dynamic_sleep(*args), expected)
                self.assertAlmostEqual(self.sleep.call_args.args[0], expected)

    def test_backoff_doubles_and_is_capped(self):
        self.uniform.return_value = 0.5
        for attempt, expected in ((0, 1.5), (3, 8.5), (10, 120)):
            with self.subTest(attempt=attempt):
                exponential_backoff(attempt)
                self.assertEqual(self.sleep.call_args.args[0], expected)


if __name__ == '__main__':
    unittest.main()
