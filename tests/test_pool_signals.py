"""Pool teardown: workers lead their own groups; a signal or an exception kills every group at once."""

import signal
import unittest
from unittest.mock import MagicMock, call, patch

from unbake import pool


def opened() -> tuple[pool.Pool, MagicMock]:
    executor = MagicMock(_processes={101: object(), 102: object()})
    with patch.object(pool, "_executor", return_value=executor):
        return pool.Pool(2, 8, 2, 1).__enter__(), executor


class PoolSignalTests(unittest.TestCase):
    def test_worker_start_leads_a_new_group(self) -> None:
        with (
            patch.object(pool.os, "setpgid") as setpgid,
            patch.object(pool, "_die_with_owner") as watch,
            patch.object(pool.resource, "setrlimit"),
        ):
            pool._cap(1)
        setpgid.assert_called_once_with(0, 0)
        watch.assert_called_once_with()

    def test_owner_is_the_fork_server_parent(self) -> None:
        with patch.object(pool.Path, "read_text", return_value="77 (py (x)) S 4242 77 77 0"):
            self.assertEqual(pool.owner(77), 4242)

    def test_a_missing_group_falls_back_to_the_worker(self) -> None:
        with (
            patch.object(pool.os, "killpg", side_effect=[None, ProcessLookupError]) as killpg,
            patch.object(pool.os, "kill") as kill,
        ):
            pool.kill_groups([101, 102])
        self.assertEqual(killpg.call_args_list, [call(101, signal.SIGKILL), call(102, signal.SIGKILL)])
        kill.assert_called_once_with(102, signal.SIGKILL)

    def test_each_signal_kills_the_groups_then_raises_and_handlers_are_restored(self) -> None:
        before = {number: signal.getsignal(number) for number in pool.SIGNALS}
        for number, raised, code in [
            (signal.SIGINT, KeyboardInterrupt, None),
            (signal.SIGTERM, SystemExit, 143),
            (signal.SIGHUP, SystemExit, 129),
        ]:
            with self.subTest(signal=number.name), patch.object(pool, "kill_groups") as killed:
                workers, executor = opened()
                self.assertEqual(signal.getsignal(number), workers._signalled)
                with self.assertRaises(raised) as caught:
                    workers._signalled(number, None)
                if code is not None:
                    self.assertEqual(caught.exception.code, code)
                killed.assert_called_once_with([101, 102])
                workers.__exit__(None, None, None)
                executor.shutdown.assert_called_with(wait=False, cancel_futures=True)
                self.assertEqual({n: signal.getsignal(n) for n in pool.SIGNALS}, before)

    def test_exit_waits_normally_and_kills_on_an_exception(self) -> None:
        for kind, killed_groups in [(None, False), (RuntimeError, True)]:
            with self.subTest(kind=kind), patch.object(pool, "kill_groups") as killed:
                workers, executor = opened()
                workers.__exit__(kind, None, None)
                self.assertEqual(killed.called, killed_groups)
                if not killed_groups:
                    executor.shutdown.assert_called_once_with(cancel_futures=True)
