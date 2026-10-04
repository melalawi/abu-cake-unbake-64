"""The project lock: one writer, a refusal that names the holder, no lock for read-only verbs."""

import os
from types import SimpleNamespace

from tests.kit import TempCase
from unbake import lock
from unbake.config import Held

READ_ONLY = {"draft", "compare", "tidy", "explain", "next", "search-variants"}
WRITERS = {"publish", "boundary", "setup", "check", "recompute", "cycle"}


class ProjectLockTests(TempCase):
    def setUp(self) -> None:
        super().setUp()
        (self.root / "build").mkdir()
        self.project = SimpleNamespace(root=self.root, build=self.root / "build")

    def test_second_holder_is_refused_with_pid_and_start(self) -> None:
        with lock.project_lock(self.project):
            with self.assertRaises(Held) as raised, lock.project_lock(self.project):
                self.fail("second holder must not enter")
        reason = raised.exception.reason
        self.assertRegex(reason, rf"^project\.lock: .*pid {os.getpid()} \(started \S+\) is writing this project$")
        self.assertEqual(raised.exception.phase, "lock")

    def test_released_lock_can_be_taken_again_and_survives_the_file(self) -> None:
        for _ in range(2):
            with lock.project_lock(self.project):
                self.assertTrue((self.root / "build" / "project.lock").is_file())

    def test_refused_holder_leaves_the_first_record_intact(self) -> None:
        with lock.project_lock(self.project):
            before = (self.root / "build" / "project.lock").read_text()
            with self.assertRaises(Held), lock.project_lock(self.project):
                pass
            self.assertEqual((self.root / "build" / "project.lock").read_text(), before)
            self.assertTrue(before.startswith(str(os.getpid())))

    # DRAFT interface: the verb table name is not fixed in INTERFACES section 12.
    def test_verb_table_splits_readers_from_writers(self) -> None:
        table = lock.READ_ONLY
        self.assertEqual(set(table), READ_ONLY)
        self.assertFalse(set(table) & WRITERS)
