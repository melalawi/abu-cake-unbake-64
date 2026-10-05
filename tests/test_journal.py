"""journal: a step's writes are all or nothing, and scratch directories of dead processes are swept."""

import os
from unittest.mock import patch

from tests.kit import TempCase
from unbake import journal


class JournalTests(TempCase):
    def setUp(self) -> None:
        super().setUp()
        self.kept, self.new = self.root / "kept.h", self.root / "new.h"
        self.kept.write_text("old\n")
        self.directory = self.root / "build" / "step.journal"

    def change(self, changes: journal.Journal) -> None:
        changes.save([self.kept, self.new])
        self.kept.write_text("changed\n")
        self.new.write_text("added\n")

    def test_an_exception_restores_every_saved_path(self) -> None:
        with self.assertRaises(RuntimeError), journal.Journal(self.directory) as changes:
            self.change(changes)
            raise RuntimeError("held")
        self.assertEqual((self.kept.read_text(), self.new.exists(), self.directory.exists()), ("old\n", False, False))

    def test_a_killed_run_is_restored_by_the_next(self) -> None:
        changes = journal.Journal(self.directory).__enter__()
        self.change(changes)  # no __exit__: the process died here
        self.assertEqual(journal.recover(self.directory), [self.kept, self.new])
        self.assertEqual((self.kept.read_text(), self.new.exists(), self.directory.exists()), ("old\n", False, False))

    def test_success_keeps_the_changes_and_drops_the_journal(self) -> None:
        with journal.Journal(self.directory) as changes:
            self.change(changes)
        self.assertEqual(
            (self.kept.read_text(), self.new.read_text(), self.directory.exists()), ("changed\n", "added\n", False)
        )

    def test_scratch_sweeps_only_dead_owners(self) -> None:
        parent = self.root / "build"
        dead, live = parent / ".declarations-999999-0", parent / f".declarations-{os.getppid()}-0"
        dead.mkdir(parents=True)
        live.mkdir()
        with patch.object(journal, "_alive", side_effect=lambda pid: pid != 999999):
            made = journal.scratch(parent, ".declarations-")
        self.assertEqual((dead.exists(), live.exists(), made.name), (False, True, f".declarations-{os.getpid()}-0"))
