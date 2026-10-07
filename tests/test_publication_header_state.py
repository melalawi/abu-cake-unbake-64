"""Real rebased headers accept committed state while retaining edit protection."""

import shutil
import subprocess
from pathlib import Path

from tests.project_fixture import ProjectCase
from unbake import inputs, land, steps
from unbake.config import Held
from unbake.project import generated_state

FIXTURE = Path(__file__).parent / "fixtures/publication_headers/rebase"


class PublicationHeaderStateTests(ProjectCase):
    versions = ("us",)

    def git(self, *args):
        return subprocess.check_output(["git", *args], cwd=self.project.root, stderr=subprocess.PIPE).decode()

    def commit(self, message):
        self.git("add", "include")
        self.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.com", "commit", "-qm", message)

    def setUp(self):
        super().setUp()
        self.include = self.project.include[-1]
        self.headers = []
        for source in sorted((FIXTURE / "before").rglob("*.h")):
            target = self.include / source.relative_to(FIXTURE / "before")
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            self.headers.append(target)
        self.git("init", "-q")
        self.commit("Original generated headers")
        steps.record(self.project, "headers", "old-input-key", steps._digests(self.project, self.headers))
        for source in (FIXTURE / "after").rglob("*.h"):
            shutil.copyfile(source, self.include / source.relative_to(FIXTURE / "after"))
        self.commit("Upstream publication headers")

    def test_committed_real_headers_reconcile_without_recompute(self):
        self.assertEqual(len(steps.altered(self.project, "headers")), 2)
        self.assertEqual(self.git("status", "--porcelain", "--", "include"), "")
        land._refuse_edited_headers(self.project)
        self.assertEqual(steps.altered(self.project, "headers"), [])
        self.assertEqual(steps.recorded(self.project, "headers"), "old-input-key")
        receipt = (self.project.build / "steps.json").read_bytes()
        land._refuse_edited_headers(self.project)
        self.assertEqual((self.project.build / "steps.json").read_bytes(), receipt)

    def test_unpublished_edits_are_refused_even_when_staged(self):
        edited = self.headers[0]
        edited.write_text(edited.read_text() + "extern int unpublished(void);\n")
        self.git("add", "include")
        with self.assertRaisesRegex(Held, "land.generated_edit") as raised:
            land._refuse_edited_headers(self.project)
        self.assertIn(edited.name, raised.exception.reason)
        self.assertNotIn(self.headers[1].name, raised.exception.reason)
        self.assertEqual(steps.altered(self.project, "headers"), [str(edited.relative_to(self.project.root))])

    def test_edit_restored_to_old_receipt_bytes_is_still_generated_not_committed(self):
        # The original generated receipt remains legitimate: rebase does not
        # turn every Git difference into a hand edit.
        source = FIXTURE / "before" / self.headers[0].relative_to(self.include)
        shutil.copyfile(source, self.headers[0])
        land._refuse_edited_headers(self.project)
        self.assertEqual(steps.altered(self.project, "headers"), [])

    def test_local_deletion_refuses_but_committed_deletion_reconciles(self):
        target = self.headers[0]
        target.unlink()
        with self.assertRaisesRegex(Held, "land.generated_edit"):
            land._refuse_edited_headers(self.project)
        self.commit("Remove obsolete generated home")
        land._refuse_edited_headers(self.project)
        self.assertEqual(steps.altered(self.project, "headers"), [])
        self.assertNotIn(str(target.relative_to(self.project.root)), steps._read(self.project)["headers"]["outputs"])

    def test_missing_untracked_output_is_never_treated_as_committed_deletion(self):
        ghost = self.include / "common/untracked.h"
        entry = steps._read(self.project)["headers"]
        steps.record(
            self.project, "headers", entry["key"], {**entry["outputs"], "include/common/untracked.h": "0" * 64}
        )
        self.assertEqual(generated_state.reconcile(self.project, "headers"), ["include/common/untracked.h"])
        self.assertFalse(ghost.exists())

    def test_symlink_with_committed_bytes_is_refused(self):
        target = self.headers[0]
        copy = self.project.build / "copy.h"
        shutil.copyfile(target, copy)
        target.unlink()
        target.symlink_to(copy)
        with self.assertRaisesRegex(Held, "land.generated_edit"):
            land._refuse_edited_headers(self.project)

    def test_generic_step_and_non_header_output_reconcile(self):
        target = self.include / "output.txt"
        target.write_text("original\n")
        steps.record(self.project, "other", "other-key", steps._digests(self.project, [target]))
        target.write_text("committed upstream\n")
        self.commit("Other generated output")
        self.assertEqual(generated_state.reconcile(self.project, "other"), [])
        self.assertEqual(steps.recorded(self.project, "other"), "other-key")
        self.assertEqual(
            steps._read(self.project)["other"]["outputs"]["include/output.txt"],
            inputs.digest(target, algorithm="sha256", reuse=True),
        )

    def test_without_readable_head_changes_still_refuse(self):
        shutil.rmtree(self.project.root / ".git")
        with self.assertRaisesRegex(Held, "land.generated_edit"):
            land._refuse_edited_headers(self.project)
