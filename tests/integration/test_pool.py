"""pool.Pool with real worker processes: result order, one retry after a crash, then TaskFailed."""

import tempfile
import unittest
from pathlib import Path

from tests.integration import workers
from unbake import pool

GIB = 1 << 30


def make_pool() -> pool.Pool:
    return pool.Pool(2, 8 * GIB, 1 * GIB, 1 * GIB)


class PoolTests(unittest.TestCase):
    def test_results_follow_item_order(self) -> None:
        items = [5, 1, 4, 2, 3, 9, 0, 7, 6, 8, 11, 10]
        with make_pool() as workers_pool:
            self.assertEqual(list(workers_pool.map(workers.double, items)), [item * 2 for item in items])

    def test_crashed_task_is_retried_once_in_a_fresh_worker(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            marker = str(Path(directory) / "crashed")
            items = [(marker, 1), (marker, -2), (marker, 3), (marker, 4)]
            with make_pool() as workers_pool:
                self.assertEqual(list(workers_pool.map(workers.crash_once, items)), [1, 2, 3, 4])
            self.assertTrue(Path(marker).exists())

    def test_second_crash_raises_task_failed_naming_the_item(self) -> None:
        with make_pool() as workers_pool, self.assertRaises(pool.TaskFailed) as raised:
            list(workers_pool.map(workers.crash_always, [1, 2]))
        self.assertIn("worker.crash", str(raised.exception))

    def test_recycle_constant_is_explicit(self) -> None:
        self.assertEqual(pool.RECYCLE_AFTER, 64)


if __name__ == "__main__":
    unittest.main()
