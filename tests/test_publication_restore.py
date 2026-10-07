"""Rejected public writes restore real payload bytes; accepted prefixes remain durable."""

import argparse
import io
from pathlib import Path
from unittest.mock import patch

from tests.project_fixture import ProjectCase
from unbake import atomic, land
from unbake.cli import publish
from unbake.cli.args import Context
from unbake.config import Held

FIXTURE = Path(__file__).parent / "fold/fixtures/shared_provider/func_80204C34_de_closed.h"


class PublicationRestoreTests(ProjectCase):
    def test_current_public_membership_write_restores_on_fold_hold_and_interrupt(self):
        from tests.layout.test_public_membership import FUZZY, PublicMembershipTests
        from unbake.fold import apply
        from unbake.journal import Journal
        from unbake.layout import modules

        for rejection in (Held("fold", "fold.fixture_hold: rejected"), KeyboardInterrupt()):
            with self.subTest(rejection=type(rejection).__name__):
                case = PublicMembershipTests()
                case.setUp()
                try:
                    before = case.layout.read_bytes()
                    args = argparse.Namespace(
                        files=[case.files[FUZZY]], original=[], require_version=None, events=False, fuzzy=True
                    )
                    context = Context("publish", args, case.project.root, None, io.StringIO(), case.host)
                    save = Journal.save
                    with (
                        patch.object(modules, "facts", side_effect=lambda p, v, current=case: current.recorded[v]),
                        patch.object(apply, "fold", side_effect=rejection) as fold,
                        patch.object(Journal, "save", autospec=True, side_effect=save) as journal,
                    ):
                        result = publish.run(context)
                    self.assertEqual(case.layout.read_bytes(), before)
                    self.assertEqual(result.data["commits"], [])
                    self.assertEqual(fold.call_count, 1)
                    self.assertEqual(case.processes.call_count, 0)
                    self.assertEqual(journal.call_count, 1)
                    self.assertEqual(journal.call_args.args[1], (case.layout,))
                    self.assertEqual(result.status, "held" if isinstance(rejection, Held) else "interrupted")
                finally:
                    case.doCleanups()

    def test_written_paths_saved_once_with_no_project_scan_and_exact_old_modes_and_deleted_files(self):
        from unbake.journal import Journal
        from unbake.project.publication_transaction import transaction

        header = self.project.include[-1] / "owner.h"
        header.write_bytes(FIXTURE.read_bytes())
        header.chmod(0o640)
        old = header.read_bytes()
        original_mtime = header.stat().st_mtime_ns
        deleted = self.project.include[-1] / "retained.h"
        deleted.write_bytes(old)
        fresh = self.project.include[-1] / "unlanded.h"
        untouched = self.project.src / "user.c"
        untouched.write_text("/* pre-existing user edit */\n")
        save = Journal.save
        with (
            patch.object(Path, "rglob", side_effect=AssertionError("no project snapshot or scan")) as scan,
            patch.object(Journal, "save", autospec=True, side_effect=save) as journal,
            self.assertRaises(Held),
            transaction(self.project),
        ):
            atomic.write(header, old + b"/* first staged write */\n", mode=0o600)
            atomic.write(header, old + b"/* second staged write */\n")
            atomic.write(fresh, old)
            atomic.remove(deleted)
            raise Held("publish", "publish.fixture_hold: consumer refused")
        self.assertEqual(journal.call_count, 3)
        self.assertEqual({args.args[1][0] for args in journal.call_args_list}, {header, fresh, deleted})
        scan.assert_not_called()
        self.assertEqual(header.read_bytes(), old)
        self.assertEqual(header.stat().st_mode & 0o777, 0o640)
        self.assertEqual(header.stat().st_mtime_ns, original_mtime)
        self.assertEqual(deleted.read_bytes(), old)
        self.assertFalse(fresh.exists())
        self.assertEqual(untouched.read_text(), "/* pre-existing user edit */\n")

    def test_symlink_directory_entry_and_referent_survive_a_rejected_write(self):
        from unbake.project.publication_transaction import transaction

        referent = self.project.include[-1] / "original.h"
        referent.write_bytes(FIXTURE.read_bytes())
        link = self.project.include[-1] / "linked.h"
        link.symlink_to("original.h")
        with self.assertRaises(KeyboardInterrupt), transaction(self.project):
            atomic.text(link, "struct Rejected {int value;};\n")
            raise KeyboardInterrupt
        self.assertTrue(link.is_symlink())
        self.assertEqual(str(link.readlink()), "original.h")
        self.assertEqual(referent.read_bytes(), FIXTURE.read_bytes())

    def test_successful_sibling_is_kept_and_failed_siblings_new_provider_is_removed(self):
        files = [self.project.work / name / f"{name}.c" for name in ("alpha", "beta")]
        for file in files:
            file.parent.mkdir(parents=True)
            file.write_text(f"int {file.stem}(void) {{return 1;}}\n")
        shared = self.project.include[-1] / "owner.h"
        fresh = self.project.include[-1] / "unlanded.h"
        shared.write_bytes(FIXTURE.read_bytes())
        accepted = shared.read_bytes() + b"/* accepted owner */\n"
        calls = []

        def action(project, host, file, on_commit=None, **kwargs):
            calls.append(file.stem)
            if file.stem == "alpha":
                atomic.write(shared, accepted)
                on_commit({"function": "alpha", "commit": "accepted", "proof": {"versions": ["us", "eu"]}})
                return "accepted"
            atomic.write(shared, b"/* unlanded replacement */\n")
            atomic.write(fresh, FIXTURE.read_bytes())
            raise Held("headers", "headers.fixture_hold: duplicate provider")

        args = argparse.Namespace(files=files, original=[], require_version=None, events=False, fuzzy=False)
        context = Context("publish", args, self.project.root, None, io.StringIO(), self.host)
        with patch.object(land, "land", side_effect=action), patch.object(land.steps, "ensure", return_value=[]):
            result = publish.run(context)
        self.assertEqual(calls, ["alpha", "beta"])
        self.assertEqual(result.data["landed"], ["alpha"])
        self.assertEqual(result.data["commits"], ["accepted"])
        self.assertEqual(list(result.data["failed"]), ["beta"])
        self.assertEqual(shared.read_bytes(), accepted)
        self.assertFalse(fresh.exists())

    def test_rejected_post_commit_maintenance_restores_only_its_writes(self):
        header = self.project.include[-1] / "owner.h"
        header.write_bytes(FIXTURE.read_bytes())
        old = header.read_bytes()
        file = self.root / "alpha.c"
        args = argparse.Namespace(files=[file], original=[], require_version=None, events=False, fuzzy=False)
        context = Context("publish", args, self.project.root, None, io.StringIO(), self.host)

        def maintenance(*args, **kwargs):
            atomic.write(header, old + b"/* rejected maintenance */\n")
            raise Held("merge", "merge.fixture_hold: maintenance refused")

        with (
            patch.object(land, "land", return_value="accepted"),
            patch.object(land.steps, "ensure", side_effect=maintenance),
        ):
            result = publish.run(context)
        self.assertEqual(result.data["commits"], ["accepted"])
        self.assertEqual(result.data["post_commit_failure"]["commit"], "accepted")
        self.assertEqual(header.read_bytes(), old)

    def test_git_commit_checkpoint_survives_a_following_exception(self):
        from unbake.project.publication_transaction import transaction

        file = self.project.src / "alpha.c"
        file.write_text("int alpha(void) {return 1;}\n")
        accepted = "int alpha(void) {return 2;}\n"
        with (
            patch.object(land, "_git", return_value=".git/index"),
            patch.object(land, "_refuse_dangling_includes"),
            self.assertRaises(KeyboardInterrupt),
            transaction(self.project),
        ):
            atomic.text(file, accepted)
            land._commit(self.project, self.host, [file], "Match alpha")
            atomic.text(file, "/* uncommitted tail */\n")
            raise KeyboardInterrupt
        self.assertEqual(file.read_text(), accepted)
