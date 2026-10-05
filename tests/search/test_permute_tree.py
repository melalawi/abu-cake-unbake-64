"""The permuter's whole process tree ends with its run, on every way out."""

import subprocess
import sys
import unittest
from typing import ClassVar
from unittest.mock import patch

from tests.kit import TempCase
from unbake import pool
from unbake.search import permute


class Fake:
    """A Popen stand-in: the command (pid 100) and the owner watcher (pid 200)."""

    started: ClassVar[list["Fake"]] = []

    def __init__(self, argv, **kwargs):  # type: ignore[no-untyped-def]
        self.argv, self.kwargs = argv, kwargs
        self.pid = 100 if len(Fake.started) == 0 else 200
        self.killed = False
        self.outcome: object = 0
        Fake.started.append(self)

    def wait(self, timeout=None):  # type: ignore[no-untyped-def]
        if self is Fake.started[0] and timeout is not None:
            if isinstance(self.outcome, BaseException):
                raise self.outcome
            return self.outcome
        return 0

    def kill(self) -> None:
        self.killed = True


class TreeTests(TempCase):
    def run_with(self, outcome: object) -> tuple[object, list[list[int]], list[Fake]]:
        Fake.started = []
        groups: list[list[int]] = []
        original = Fake.__init__

        def init(self, argv, **kwargs):  # type: ignore[no-untyped-def]
            original(self, argv, **kwargs)
            if len(Fake.started) == 1:
                self.outcome = outcome

        with (
            patch.object(Fake, "__init__", init),
            patch.object(permute.subprocess, "Popen", Fake),
            patch.object(pool, "kill_groups", lambda pids: groups.append(list(pids))),
        ):
            try:
                result: object = permute._run(["permuter"], self.root, {}, 5.0, self.root / "log")
            except BaseException as error:
                result = error
        return result, groups, list(Fake.started)

    def test_a_normal_exit_still_ends_the_group(self) -> None:
        result, groups, started = self.run_with(0)
        self.assertEqual((result.ran, result.returncode), (True, 0))  # type: ignore[attr-defined]
        self.assertEqual(groups, [[100]])
        self.assertTrue(started[1].killed)

    def test_the_deadline_ends_the_group(self) -> None:
        result, groups, started = self.run_with(subprocess.TimeoutExpired("permuter", 5.0))
        self.assertEqual((result.ran, result.returncode), (True, None))  # type: ignore[attr-defined]
        self.assertEqual(groups, [[100]])
        self.assertTrue(started[1].killed)

    def test_any_exception_ends_the_group_and_propagates(self) -> None:
        result, groups, started = self.run_with(KeyboardInterrupt())
        self.assertIsInstance(result, KeyboardInterrupt)
        self.assertEqual(groups, [[100]])
        self.assertTrue(started[1].killed)

    def test_the_command_leads_its_own_group_and_a_watcher_outside_it_names_owner_and_group(self) -> None:
        _, _, started = self.run_with(0)
        self.assertTrue(started[0].kwargs["start_new_session"])
        argv = started[1].argv
        self.assertEqual(argv[0], sys.executable)
        self.assertEqual(argv[-1], "100")
        self.assertTrue(started[1].kwargs["start_new_session"])
        self.assertEqual(argv[-2].isdigit(), True)

    def test_a_spent_budget_starts_nothing(self) -> None:
        Fake.started = []
        with patch.object(permute.subprocess, "Popen", Fake):
            result = permute._run(["permuter"], self.root, {}, 0, self.root / "log")
        self.assertEqual((result.ran, Fake.started), (False, []))


if __name__ == "__main__":
    unittest.main()
