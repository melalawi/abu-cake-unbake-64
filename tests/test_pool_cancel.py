"""Cancelling a task's future cancels it only while it is queued; a running task finishes and its result stands."""

import unittest
from concurrent.futures import Future

from unbake import pool


class OuterCancelTests(unittest.TestCase):
    def test_a_running_task_refuses_cancellation_and_a_queued_one_accepts_it(self) -> None:
        running: Future[int] = Future()
        running.set_running_or_notify_cancel()
        queued: Future[int] = Future()
        cases = {"running": (running, False), "queued": (queued, True)}
        for name, (inner, cancelled) in cases.items():
            with self.subTest(name):
                outer = pool._Outer(inner)
                self.assertEqual(outer.cancel(), cancelled)
                self.assertEqual(outer.cancelled(), cancelled)
                self.assertEqual(inner.cancelled(), cancelled)
        running.set_result(1)
