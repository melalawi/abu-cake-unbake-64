"""Guard process creation and preserve batch executor semantics with a fake."""

import subprocess
import sys
import unittest
from unittest.mock import patch

from tests import runner
from tests.process_fakes import Pool
from unbake.match import forked, reporting


class UnitBoundaries(unittest.TestCase):
    def test_subprocess_is_rejected_before_launch(self):
        with self.assertRaisesRegex(AssertionError, "mock the external tool boundary"):
            subprocess.run(["forbidden-fixture-program"])

    def test_audit_catches_process_routes_even_without_popen(self):
        for event in runner._EVENTS:
            with self.subTest(event=event), self.assertRaisesRegex(AssertionError, "creation is forbidden"):
                sys.audit(event)
        runner.audit("unrelated-fixture-event", ())

    def test_fanout_keeps_input_order_receipts_and_cleans_shared_work(self):
        def work(shared, item):
            reporting.learn(f"receipt {item}")
            return shared + item

        with patch.object(forked, "ProcessPoolExecutor", wraps=Pool) as executor:
            outcomes = list(forked.ordered(work, 10, [3, 1, 2], 8))
        self.assertEqual(outcomes, [(["receipt 3"], 13), (["receipt 1"], 11), (["receipt 2"], 12)])
        self.assertEqual(executor.call_args.kwargs["max_workers"], min(3, forked.MAX_WORKERS))
        self.assertEqual(executor.call_args.kwargs["mp_context"].get_start_method(), "fork")
        self.assertIsNone(forked._work)
