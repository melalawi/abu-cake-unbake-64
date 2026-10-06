"""pool.run batches items into jobs by one rule; a worker memory failure names the cap and the peak."""

import unittest
from collections.abc import Iterator, Sequence
from concurrent.futures import Future
from typing import Any
from unittest.mock import patch

from unbake import effort, pool

GIB = 1 << 30


def double(value: int) -> int:
    return value * 2


class Batching(unittest.TestCase):
    def run_with(self, workers: int, count: int) -> tuple[list[int], list[int], list[str | None]]:
        sizes: list[int] = []
        charged: list[str | None] = []

        def serial(self: pool.Pool, fn: Any, jobs: Sequence[Any], *, charge: str | None = None) -> Iterator[Any]:
            charged.append(charge)
            for job in jobs:
                sizes.append(len(job[1]))
                yield fn(job)

        with patch.object(pool.Pool, "_fresh"), patch.object(pool.Pool, "map", serial):
            result = pool.Pool(workers, 64 * GIB, 1 * GIB, 1 * GIB).run(double, list(range(count)))
        return result, sizes, charged

    def test_results_stay_in_item_order_and_effort_is_charged_to_the_item_function(self) -> None:
        result, _, charged = self.run_with(4, 100)
        self.assertEqual(result, [value * 2 for value in range(100)])
        self.assertEqual(charged, [effort.name_of(double)])

    def test_a_large_fill_is_capped_per_job(self) -> None:
        _, sizes, _ = self.run_with(10, 3022)
        self.assertEqual(max(sizes), pool.ITEMS_PER_JOB)
        self.assertEqual(sum(sizes), 3022)

    def test_a_small_fill_still_spreads_over_every_worker(self) -> None:
        _, sizes, _ = self.run_with(4, 10)
        self.assertEqual(sizes, [1] * 10)
        _, sizes, _ = self.run_with(4, 40)
        self.assertEqual(sizes, [3] * 13 + [1])

    def test_one_job_per_item_is_never_zero_sized(self) -> None:
        _, sizes, _ = self.run_with(8, 1)
        self.assertEqual(sizes, [1])


class MemoryFailure(unittest.TestCase):
    def test_fresh_retry_names_the_actual_unit_and_measured_memory(self) -> None:
        workers = pool.Pool(2, 8 * GIB, GIB, 512_000_000)
        fault = {"action": "types", "identity": {"source": "src/actual.c", "functions": ["actual"], "versions": ["de"]},
                 "allocation": "declarations.py:scan", "peak_rss_bytes": 123456, "cpu_seconds": 2.0}
        failed = Future()
        failed.set_result((None, 2.0, 123456, {}, pool.WorkerMemory(fault)))
        with patch.object(workers, "_submit", return_value=failed), patch.object(workers, "_fresh") as fresh:
            with self.assertRaises(pool.TaskFailed) as raised:
                list(workers.map(double, ["batch-first"]))
        self.assertEqual(fresh.call_count, 1)
        self.assertIn("actual.c", raised.exception.reason)
        self.assertNotIn("batch-first", raised.exception.reason)
        self.assertEqual(raised.exception.fault["configured_cap_bytes"], 512_000_000)
        self.assertEqual(raised.exception.fault["peak_rss_bytes"], 123456)

    def test_a_crash_names_the_cap_without_inventing_a_peak(self) -> None:
        from concurrent.futures.process import BrokenProcessPool

        workers = pool.Pool(2, 8 * GIB, 1 * GIB, 512_000_000)
        failed: Future[Any] = Future()
        failed.set_exception(BrokenProcessPool())
        with (
            patch.object(workers, "_submit", lambda fn, item: failed),
            patch.object(workers, "_fresh"),
            self.assertRaises(pool.TaskFailed) as raised,
        ):
            list(workers.map(double, [1]))
        self.assertIn("worker.crash", str(raised.exception))
        self.assertNotIn("peak", str(raised.exception))


if __name__ == "__main__":
    unittest.main()


class PhysicalRetirement(unittest.TestCase):
    def test_existing_retirement_budget_counts_items_inside_batches(self):
        with patch.object(pool, 'ProcessPoolExecutor') as executor:
            pool._executor(2, 512_000_000, 24)
        child_jobs = executor.call_args.kwargs['max_tasks_per_child']
        self.assertLessEqual(child_jobs * 24, pool.RECYCLE_AFTER)
        self.assertEqual(executor.call_args.kwargs['initargs'], (512_000_000,))
