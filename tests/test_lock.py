"""The project lock: one writer, a refusal that names the holder named by its command."""

import os

from tests.kit import TempCase
from unbake import lock
from unbake.config import Held


class ProjectLockTests(TempCase):
    def setUp(self) -> None:
        super().setUp()
        (self.root / "build").mkdir()

    def test_second_holder_is_refused_with_pid_and_start(self) -> None:
        with (
            lock.project_lock(self.root, "publish"),
            self.assertRaises(Held) as raised,
            lock.project_lock(self.root, "publish"),
        ):
            self.fail("second holder must not enter")
        reason = raised.exception.reason
        self.assertRegex(reason, rf"^project\.lock: publish pid {os.getpid()} \(started \S+\) is writing this project$")
        self.assertEqual(raised.exception.phase, "lock")

    def test_released_lock_can_be_taken_again_and_survives_the_file(self) -> None:
        for _ in range(2):
            with lock.project_lock(self.root, "publish"):
                self.assertTrue((self.root / "build" / "project.lock").is_file())

    def test_refused_holder_leaves_the_first_record_intact(self) -> None:
        with lock.project_lock(self.root, "publish"):
            before = (self.root / "build" / "project.lock").read_text()
            with self.assertRaises(Held), lock.project_lock(self.root, "publish"):
                pass
            self.assertEqual((self.root / "build" / "project.lock").read_text(), before)
            self.assertTrue(before.startswith(f"publish pid {os.getpid()} "))

    # DRAFT interface: the verb table name is not fixed in INTERFACES section 12.
