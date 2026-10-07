"""A slow first result must not strand an otherwise free admitted worker."""

import threading
import unittest
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from unittest.mock import patch

from unbake import pool


class CompletionTests(unittest.TestCase):
    def test_known_memory_failure_starts_no_unchanged_recovery_worker(self):
        calls = []

        class Executor:
            def submit(self, fn, task):
                _, item = task
                calls.append(item)
                future = Future()
                fault = MemoryError() if item == 0 else None
                future.set_result((item, 0.0, 0, {}, fault))
                return future

            def shutdown(self, **kwargs):
                pass

        owner = pool.Pool(2, 8_000_000_000, 4_000_000_000, 512_000_000)
        owner._executor = Executor()
        with (
            patch.object(pool, "_executor", return_value=Executor()) as restarted,
            self.assertRaises(pool.TaskFailed) as caught,
        ):
            list(owner.map(int, [0, 1]))
        self.assertEqual((caught.exception.key, calls, restarted.call_count), ("worker.memory", [0, 1], 0))

    def test_a_finished_sibling_is_not_repeated_after_another_worker_crashes(self):
        calls = []

        def submit(fn, task):
            _, item = task
            calls.append(item)
            future = Future()
            if item == 0 and calls.count(0) == 1:
                future.set_exception(BrokenProcessPool())
            else:
                future.set_result((item, 0.0, 0, {}, None))
            return future

        owner = pool.Pool(2, 8_000_000_000, 4_000_000_000, 512_000_000)
        with patch.object(owner, "_submit", submit), patch.object(owner, "_fresh"):
            self.assertEqual(list(owner.map(int, [0, 1])), [0, 1])
        self.assertEqual(calls.count(0), 2)
        self.assertEqual(calls.count(1), 1)

    def test_free_worker_continues_while_results_remain_in_input_order(self):
        progressed = threading.Event()
        active = 0
        peak = 0
        lock = threading.Lock()

        def action(item):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            try:
                if item == 0:
                    return progressed.wait(1.0)
                if item == 4:
                    progressed.set()
                return item
            finally:
                with lock:
                    active -= 1

        # Threads stand in only for the native process boundary; admission and
        # result delivery exercise the real Pool.map implementation.
        owner = pool.Pool(2, 8_000_000_000, 4_000_000_000, 512_000_000)
        with ThreadPoolExecutor(max_workers=2) as executor, patch.object(owner, "_submit", executor.submit):
            result = list(owner.map(action, list(range(5))))
        self.assertEqual(result, [True, 1, 2, 3, 4])
        self.assertLessEqual(peak, 2)

    def test_failure_is_reported_in_input_order_even_when_later_failure_finishes_first(self):
        from unbake.config import Held
        from unbake.process import named

        second_done = threading.Event()

        def action(item):
            if item == 0:
                second_done.wait(1.0)
                raise Held(named("fixture.refusal", "first input refused", owner="fixture", stage="test"))
            second_done.set()
            raise Held(named("fixture.refusal", "later input refused", owner="fixture", stage="test"))

        owner = pool.Pool(2, 8_000_000_000, 4_000_000_000, 512_000_000)
        with (
            ThreadPoolExecutor(max_workers=2) as executor,
            patch.object(owner, "_submit", executor.submit),
            self.assertRaisesRegex(Held, "first input refused"),
        ):
            list(owner.map(action, [0, 1]))
