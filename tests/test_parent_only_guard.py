import inspect
import unittest

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


if __name__ == "__main__":
    unittest.main()
