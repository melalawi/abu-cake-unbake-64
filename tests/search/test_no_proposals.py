"""A method with nothing to propose ends with the starting source; only an exhausted budget is refused."""

import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from unbake.search import core


class Silent:
    name = "silent"

    def propose(self, expanded, trial, context):  # type: ignore[no-untyped-def]
        return iter(())


class NoProposals(unittest.TestCase):
    def run_core(self, seconds: float, clock: float | None = None) -> core.SearchResult:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "f.c"
            source.write_text("int f(void) { return 0; }\n")
            compare = SimpleNamespace(identical_words=3)
            trial = SimpleNamespace(
                compares={"us": compare},
                identical_everywhere=False,
                source_sha256="a",
                best_percent=75.0,
                function="f",
                next_command="n",
                exact=False,
            )
            policy = SimpleNamespace(search_beam=2, stall_trials=3)
            real = time.monotonic
            patches = [
                patch.object(core, "measure", lambda *a, **k: trial),
                patch.object(core, "preprocess", lambda *a, **k: ""),
                patch.object(core, "_focus_lines", lambda *a: ()),
                patch.object(core, "measured_candidate_rank", lambda *a: (False, 0, 0, 0.0)),
                patch.object(core.explain, "allocation", lambda *a: SimpleNamespace(differences=())),
            ]
            if clock is not None:
                patches.append(patch.object(core.time, "monotonic", lambda: real() + clock))
            for item in patches:
                item.start()
            try:
                return core.run(SimpleNamespace(), policy, source, [Silent()], root / "out", seconds)  # type: ignore[arg-type]
            finally:
                for item in patches:
                    item.stop()

    def test_nothing_proposed_within_budget_returns_the_start(self) -> None:
        result = self.run_core(30.0)
        self.assertEqual(result.trial.compares["us"].identical_words, 3)
        self.assertEqual(result.trial.source_sha256, "a")

    def test_no_mutation_budget_returns_the_measured_baseline(self) -> None:
        # Baseline/context setup has its own clock scope; an expired mutation
        # budget still returns that measured baseline with zero mutations.
        ticks = iter(range(10_000))
        with patch.object(core.time, "monotonic", lambda: next(ticks) * 100.0):
            result = self.run_core(5.0)
        self.assertEqual(result.trials, 1)
        self.assertEqual(result.trial.source_sha256, "a")


if __name__ == "__main__":
    unittest.main()
