"""Cycle stop conditions: idle time never runs while a draft, compare or land is in flight."""

import unittest
from unittest import mock

from unbake.cycle import engine


def rows(*stages: str) -> dict[str, engine.Row]:
    return {f"f{i}": engine.Row(f"f{i}", 16, ("us",), False, stage=stage) for i, stage in enumerate(stages)}


class StopTests(unittest.TestCase):
    def test_idle(self) -> None:
        # (seconds since start, stages) -> reached
        steps = [
            (0, ("comparing",), False),
            (500, ("comparing",), False),  # a compare longer than the idle window
            (501, ("waiting for edit",), False),  # idle starts when it finishes
            (560, ("waiting for edit",), False),
            (561, ("waiting for edit",), True),
        ]
        with mock.patch.object(engine.time, "monotonic", return_value=0.0) as clock:
            stop = engine.Stop("idle:60")
            for now, stages, reached in steps:
                with self.subTest(now=now, stages=stages):
                    clock.return_value = float(now)
                    self.assertEqual(stop.reached(rows(*stages)), reached)

    def test_busy_blocks_without_timeout_and_after_is_wall_time(self) -> None:
        with mock.patch.object(engine.time, "monotonic", return_value=0.0) as clock:
            idle, after = engine.Stop("idle:60"), engine.Stop("after:1")
            clock.return_value = 61.0
            self.assertIsNone(idle.timeout(rows("landing", "waiting for edit")))
            self.assertTrue(after.reached(rows("comparing")))


if __name__ == "__main__":
    unittest.main()
