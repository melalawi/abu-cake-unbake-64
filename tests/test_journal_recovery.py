"""Recovery distinguishes the live owner and preserves interrupted operation archives."""

import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tests.kit import TESTS, TempCase
from tests.project_fixture import ProjectCase
from unbake import atomic, build, journal, process, steps
from unbake.config import Held

FIXTURE = json.loads((TESTS / "fixtures/journal_split_state.json").read_text())


def abandon(changes):
    changes.recording.__exit__(None, None, None)
    journal._current.reset(changes.token)


class LiveRecoveryTests(ProjectCase):
    def test_normal_check_and_ensure_keep_the_active_boundary_until_its_owner_commits(self):
        output = self.project.root / "units.mk"
        output.write_text("before\n")
        directory = self.project.build / "boundary.journal"
        prepared = SimpleNamespace(assert_current=lambda project: None)

        def backend(*args, **kwargs):
            self.assertEqual(output.read_text(), "after\n")
            self.assertTrue((directory / journal.INDEX).is_file())
            self.assertFalse(directory.with_name(directory.name + ".archive").exists())
            self.assertIs(journal.current(), changes)
            return []

        with (
            patch.object(steps, "prepare", return_value=prepared),
            patch.object(steps, "_ensure", side_effect=backend) as ensure,
            patch("unbake.project.hygiene.tracked_findings", return_value=[]),
            patch.object(build, "python_visible", return_value=None),
            patch.object(process, "run_native") as native,
            journal.Journal(directory, root=self.project.root) as changes,
        ):
            atomic.text(output, "after\n")
            self.assertEqual(steps.ensure(self.project, self.host, ["buildfiles"]), [])
            result = build.check(self.project, self.host, files_only=False)
            self.assertTrue(result.ok and result.built)
            self.assertEqual(ensure.call_count, 2)
            native.assert_called_once()
        self.assertEqual(output.read_text(), "after\n")
        self.assertFalse(directory.exists())
        archives = list(directory.with_name(directory.name + ".archive").iterdir())
        self.assertEqual(len(archives), 1)
        self.assertEqual(json.loads((archives[0] / journal.INDEX).read_text())["state"], "committed")

    def test_same_ensure_api_recovers_an_abandoned_installed_boundary(self):
        output = self.project.root / "units.mk"
        output.write_text("before\n")
        directory = self.project.build / "boundary.journal"
        changes = journal.Journal(directory, root=self.project.root).__enter__()
        changes.save([output])
        output.write_text("after\n")
        abandon(changes)
        with patch.object(steps, "_ensure", return_value=[]):
            steps.ensure(self.project, self.host, ["buildfiles"])
        self.assertEqual(output.read_text(), "before\n")
        self.assertFalse(directory.exists())

    def test_public_recompute_recovers_through_the_same_ensure_owner(self):
        import io

        from tests.kit import with_value
        from unbake.cli import main

        output = self.project.root / "units.mk"
        output.write_text("before\n")
        directory = self.project.build / "boundary.journal"
        changes = journal.Journal(directory, root=self.project.root).__enter__()
        atomic.text(output, "after\n")
        abandon(changes)
        host = with_value(self.host, "resources.domain", "standalone")
        with (
            patch.object(main.config, "load_host", return_value=host),
            patch.object(steps, "_ensure", return_value=[]),
        ):
            result = main._run(["--project", str(self.project.root), "recompute", "buildfiles"], io.StringIO())
        self.assertEqual(result.status, "ok")
        self.assertEqual(output.read_text(), "before\n")
        self.assertFalse(directory.exists())

    def test_default_public_compare_recovers_before_measurement_and_preserves_live_owner(self):
        import io

        from tests.kit import with_value
        from unbake.cli import main
        from unbake.layout import split
        from unbake.work import compare
        from unbake.work.score import measure_words

        directory = self.project.build / "boundary.journal"
        output = self.project.root / "units.mk"
        draft = self.project.work / "alpha/alpha.c"
        draft.parent.mkdir(parents=True)
        draft.write_text("int alpha(void) { return 1; }\n")
        host = with_value(self.host, "resources.domain", "standalone")
        for live in (False, True):
            with self.subTest(live=live):
                output.write_text("before\n")
                changes = journal.Journal(directory, root=self.project.root).__enter__()
                atomic.text(output, "after\n")
                if not live:
                    abandon(changes)

                def measure(project, policy, file, live=live):
                    self.assertEqual(output.read_text(), "after\n" if live else "before\n")
                    self.assertEqual(directory.exists(), live)
                    return compare.Compared(
                        "alpha",
                        file,
                        hashlib.sha256(file.read_bytes()).hexdigest(),
                        {
                            version: measure_words(
                                version,
                                split.words(project, split.functions(project, version)[0]),
                                split.words(project, split.functions(project, version)[0]),
                            )
                            for version in project.versions
                        },
                        compiler="ido-7.1",
                    )

                try:
                    with (
                        patch.object(main.config, "load_host", return_value=host),
                        patch.object(steps, "_ensure", return_value=[]),
                        patch.object(compare, "measure", side_effect=measure) as measured,
                    ):
                        result = main._run(["--project", str(self.project.root), "compare", str(draft)], io.StringIO())
                    self.assertEqual(result.status, "ok", result.document())
                    measured.assert_called_once()
                finally:
                    if live:
                        changes.__exit__(None, None, None)
                self.assertFalse(directory.exists())

    def test_nested_owners_keep_the_live_boundary_and_reject_a_different_root(self):
        directory = self.project.build / "boundary.journal"
        with journal.Journal(directory, root=self.project.root) as changes:
            with journal.transaction(self.project) as nested:
                self.assertIs(nested, changes)
                self.assertEqual(journal.recover_all(self.project), [])
            alien = SimpleNamespace(root=self.root, build=self.root / "build")
            with self.assertRaisesRegex(Held, "owning root"):
                journal.recover(directory, root=alien.root)
            with self.assertRaisesRegex(Held, "owning root"), journal.transaction(alien):
                self.fail("different root was accepted")

    def test_a_live_owner_does_not_hide_another_abandoned_journal(self):
        output = self.project.root / "units.mk"
        output.write_text("before\n")
        abandoned = self.project.build / "step.journal"
        stale = journal.Journal(abandoned, root=self.project.root).__enter__()
        stale.save([output])
        output.write_text("after\n")
        abandon(stale)
        # Construct the second owner only after saving the abandoned state; entering it recovers stale.
        with journal.Journal(self.project.build / "boundary.journal", root=self.project.root):
            self.assertEqual(output.read_text(), "before\n")
            self.assertFalse(abandoned.exists())


class ArchiveRecoveryTests(TempCase):
    def stranded(self):
        root = self.root / str(getattr(self, "sequence", 0))
        self.sequence = getattr(self, "sequence", 0) + 1
        root.mkdir()
        directory = root / "build/boundary.journal"
        changes = journal.Journal(directory, root=root).__enter__()
        for row in FIXTURE["outputs"]:
            output = root / row["path"]
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(bytes.fromhex(row["before_hex"]))
            changes.save([output])
            output.write_text("interrupted boundary\n")
        changes.state = FIXTURE["archived_state"]
        changes.persist()
        document = changes.document()
        abandon(changes)
        target = directory.with_name(directory.name + ".archive") / document["operation_id"]
        target.parent.mkdir()
        os.rename(directory, target)
        self.assertEqual(json.loads((target / journal.INDEX).read_text())["state"], FIXTURE["archived_state"])
        directory.mkdir()
        document["state"] = FIXTURE["active_state"]
        (directory / journal.INDEX).write_text(json.dumps(document))
        return root, directory, target, document

    def test_recorded_bt_installed_to_committed_transition_uses_verified_archived_beforeimages(self):
        root, directory, archive, document = self.stranded()
        before = {row["backup"]: (archive / row["backup"]).read_bytes() for row in document["outputs"]}
        self.assertEqual(journal.recover(directory, root=root), [])
        self.assertFalse(directory.exists())
        self.assertEqual(json.loads((archive / journal.INDEX).read_text()), document)
        self.assertEqual({name: (archive / name).read_bytes() for name in before}, before)
        for row in document["outputs"]:
            self.assertEqual(hashlib.sha256(before[row["backup"]]).hexdigest(), row["backup_sha256"])
            self.assertEqual(Path(row["path"]).read_text(), "interrupted boundary\n")
        snapshots = [path for path in archive.iterdir() if path.is_dir()]
        self.assertEqual(len(snapshots), 1)
        previous = json.loads((snapshots[0] / "prior-index.json").read_text())
        self.assertEqual(previous["state"], FIXTURE["archived_state"])
        self.assertEqual(journal.recover(directory, root=root), [])

    def test_identity_inventory_state_and_beforeimage_mismatches_hold_without_mutation(self):
        def root_change(document):
            document["root"] += "-other"

        def identity_change(document):
            document["operation_id"] = "f" * 32

        def inventory_change(document):
            document["outputs"][0]["mode"] ^= 0o100

        def unsafe_state(document):
            document["state"] = "prepared"

        def acceptance_change(document):
            document["git_commit"] = "a" * 40

        for change in (root_change, identity_change, inventory_change, unsafe_state, acceptance_change):
            with self.subTest(change=change.__name__):
                root, directory, archive, document = self.stranded()
                change(document)
                (directory / journal.INDEX).write_text(json.dumps(document))
                before = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
                with self.assertRaises(Held):
                    journal.recover(directory, root=root)
                self.assertEqual({p: p.read_bytes() for p in before}, before)
                self.assertTrue(directory.exists() and archive.exists())
        root, directory, archive, document = self.stranded()
        (archive / document["outputs"][0]["backup"]).write_bytes(b"damaged")
        before = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
        with self.assertRaisesRegex(Held, "before-image"):
            journal.recover(directory, root=root)
        self.assertEqual({p: p.read_bytes() for p in before}, before)
