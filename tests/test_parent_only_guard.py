import inspect
import json
import tempfile
import unittest
from pathlib import Path

from unbake import effort, steps
from unbake.tui import progress


class PooledStepRegistry(unittest.TestCase):
    def test_heavy_steps_hand_their_workers_to_the_pool(self) -> None:
        work = steps.pooled_work()
        self.assertEqual({"buildfiles", "headers"}, set(work))
        for step, functions in work.items():
            self.assertIn(step, steps.STEPS)
            for fn in functions:
                module = inspect.getmodule(fn)
                self.assertEqual(fn, getattr(module, fn.__name__), f"{step}: {fn.__name__} must be importable")
                self.assertIn(
                    f"pool.run(host, {fn.__name__}" if step == "buildfiles" else f"{fn.__name__},",
                    inspect.getsource(module or steps),
                )

    def test_stage_over_threshold_in_parent_is_named_a_finding(self) -> None:
        self.assertTrue(effort.parent_only("x", effort.PARENT_ITEMS + 1, False) is False)
        effort.expect_workers(4)
        try:
            self.assertTrue(effort.parent_only("x", effort.PARENT_ITEMS + 1, False))
            self.assertFalse(effort.parent_only("x", effort.PARENT_ITEMS + 1, True))
            self.assertFalse(effort.parent_only("x", 3, False))
            mark = effort.mark()
            with progress.task("Looping units", effort.PARENT_ITEMS + 1):
                pass
            names = [row[0] for row in effort.since(mark).stages]
        finally:
            effort.expect_workers(1)
        self.assertIn(f"{effort.PARENT_ONLY}Looping units ({effort.PARENT_ITEMS + 1} items)", names)

    def test_stage_over_thirty_seconds_at_one_core_is_parent_only(self) -> None:
        effort.expect_workers(4)
        try:
            self.assertTrue(effort.parent_only("x", 3, False, effort.PARENT_SECONDS + 1, effort.PARENT_SECONDS + 1))
            self.assertFalse(effort.parent_only("x", 3, False, effort.PARENT_SECONDS + 1, 8 * effort.PARENT_SECONDS))
            self.assertFalse(effort.parent_only("x", 3, False, effort.PARENT_SECONDS - 1, 1.0))
            self.assertFalse(effort.parent_only("x", 3, True, 300.0, 300.0))
        finally:
            effort.expect_workers(1)

    def test_stages_are_written_as_they_finish(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            log = Path(root) / "build" / "stages.jsonl"
            log.parent.mkdir()
            effort.stage_log(log)
            try:
                with progress.task("Alpha"), self.assertRaises(RuntimeError), progress.task("Beta"):
                    raise RuntimeError("killed mid-stage")
            finally:
                effort.stage_log(None)
            names = [json.loads(line)["stage"] for line in log.read_text().splitlines()]
        self.assertEqual(["Alpha > Beta", "Alpha", "Alpha (own)"], names)


if __name__ == "__main__":
    unittest.main()
