"""Integration tests start real processes; the unit runner (bin/test) must not load them."""

import os
import unittest


def load_tests(loader: unittest.TestLoader, tests: unittest.TestSuite, pattern: str | None) -> unittest.TestSuite:
    if not os.environ.get("UNBAKE_INTEGRATION"):
        return unittest.TestSuite()
    here = os.path.dirname(__file__)
    tests.addTests(loader.discover(here, pattern or "test*.py"))
    return tests
