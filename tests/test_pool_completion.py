"""A slow first result must not strand an otherwise free admitted worker."""

import threading
import unittest
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from unittest.mock import patch

from unbake import pool


class CompletionTests(unittest.TestCase):
    def test_memory_recycle_does_not_discard_an_unstarted_sibling(self):
        queued = Future()
        calls = []

        class Executor:
            def submit(self, fn, task):
                _, item = task
                calls.append(item)
                if item == 1:
                    return queued
                future = Future()
                fault = MemoryError() if calls.count(0) == 1 else None
                future.set_result((item, 0.0, 0, {}, fault))
                return future

            def shutdown(self, *, cancel_futures=False):
                if cancel_futures:
                    queued.cancel()
                else:
                    queued.set_result((1, 0.0, 0, {}, None))

        owner = pool.Pool(2, 8_000_000_000, 4_000_000_000, 512_000_000)
        owner._executor = Executor()
        with patch.object(pool, "_executor", return_value=Executor()):
            self.assertEqual(list(owner.map(int, [0, 1])), [0, 1])
        self.assertEqual(calls, [0, 1, 0])

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

        second_done = threading.Event()

        def action(item):
            if item == 0:
                second_done.wait(1.0)
                raise Held("test", "first input refused")
            second_done.set()
            raise Held("test", "later input refused")

        owner = pool.Pool(2, 8_000_000_000, 4_000_000_000, 512_000_000)
        with (
            ThreadPoolExecutor(max_workers=2) as executor,
            patch.object(owner, "_submit", executor.submit),
            self.assertRaisesRegex(Held, "first input refused"),
        ):
            list(owner.map(action, [0, 1]))
