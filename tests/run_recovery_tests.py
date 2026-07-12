#!/usr/bin/env python3
"""Run the tracked deterministic recovery tests."""

import argparse
import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


UNIT_MODULES = (
    'tests.test_arctic_shift',
    'tests.test_pullpush_metadata',
    'tests.test_recovery_negative_cache',
    'tests.test_recovery_selection',
    'tests.test_retired_recovery_providers',
)
INTEGRATION_MODULES = ('tests.test_recovery_integration',)
PROVIDER_MODULES = {
    'wayback': ('tests.test_recovery_negative_cache', 'tests.test_recovery_selection'),
    'pullpush': ('tests.test_pullpush_metadata',),
    'reddit_preview': ('tests.test_retired_recovery_providers',),
    'reveddit': ('tests.test_retired_recovery_providers',),
}


def main(argv=None):
    """Run the selected tracked recovery tests and return a process status."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('-v', '--verbose', action='store_true')
    parser.add_argument('--unit-only', action='store_true')
    parser.add_argument('--no-integration', action='store_true')
    parser.add_argument('--no-performance', action='store_true')
    parser.add_argument('--provider', choices=PROVIDER_MODULES)
    args = parser.parse_args(argv)

    if args.provider:
        module_names = PROVIDER_MODULES[args.provider]
    else:
        module_names = list(UNIT_MODULES)
        if not args.unit_only and not args.no_integration:
            module_names.extend(INTEGRATION_MODULES)

    suite = unittest.defaultTestLoader.loadTestsFromNames(module_names)
    result = unittest.TextTestRunner(verbosity=2 if args.verbose else 1).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == '__main__':
    sys.exit(main())
