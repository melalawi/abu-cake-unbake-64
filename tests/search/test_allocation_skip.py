"""Unsupported allocator evidence keeps the baseline and records a named search skip."""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from unbake.compilers.families.gcc.allocation import allocation
from unbake.search import core


class AllocationSkip(unittest.TestCase):
    def test_real_parser_refusal_is_recorded_without_running_the_generator(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "f.c"
            source.write_text("int f(void) { return 0; }\n")
            trial = SimpleNamespace(
                compares={"us": SimpleNamespace(identical_words=3)},
                identical_everywhere=False,
                source_sha256="a",
                best_percent=75.0,
                function="f",
                exact=False,
            )
            generator = SimpleNamespace(name="order", propose=lambda *a: self.fail("unsupported context used"))
            dumps = {
                "lreg": "Register 60 used 3 times across 5 insns",
                "greg": ";; 1 regs to allocate: 60 (new-format)\n;; Register dispositions:\n60 in 2\n",
            }
            with (
                patch.object(core, "measure", return_value=trial),
                patch.object(core, "preprocess", return_value="int f(void) { return 0; }"),
                patch.object(core, "measured_candidate_rank", return_value=(False, 0, 0, 0.0)),
                patch.object(core.explain, "allocation", side_effect=lambda *a: allocation(dumps)),
            ):
                result = core.run(
                    SimpleNamespace(),
                    SimpleNamespace(search_beam=2, stall_trials=3),
                    source,
                    [generator],
                    root / "out",
                    30.0,
                )
            self.assertEqual(result.trials, 1)
            self.assertEqual(result.trial, trial)
            self.assertEqual(len(result.skips), 1)
            skip = result.skips[0]
            self.assertEqual(
                (skip["key"], skip["version"], skip["generator"], skip["source_sha256"]),
                ("dumps.greg.unknown_line", "us", "order", "a"),
            )
            rows = [json.loads(line) for line in result.steps.read_text().splitlines()]
            self.assertEqual(rows[-1]["kind"], "allocation.skip")
            self.assertEqual(rows[-1]["refusal"], skip["reason"])
