"""Require all contract tests to run, including when copied into another project."""

import unittest


def main():
    suite = unittest.defaultTestLoader.loadTestsFromNames([
        "tests.test_contract",
        "tests.test_mutating_contract",
    ])
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.testsRun and result.wasSuccessful() and not result.skipped else 1


if __name__ == "__main__":
    raise SystemExit(main())
