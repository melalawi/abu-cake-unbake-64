"""Actual stale qualified/current source projection through the ordinary CI entrypoint."""

import hashlib
import json
import os
import sys
from unittest.mock import patch

from tests.test_retained_source_state import CURRENT, NAME, OLD, RetainedSourceStateTests
from unbake.config import Held
from unbake.report import state, verify
from unbake.work import attempts


class CurrentCIInventoryCases(RetainedSourceStateTests):
    def stale_projection(self):
        # Captured immutable 12670 qualification preceded the retained provider rewrite.
        self.source.write_bytes(OLD)
        self.generate()
        self.source.write_bytes(CURRENT)
        with self.assertRaisesRegex(Held, "report.semantic"):
            verify.validate(self.project)
        # Once generated, the normal CI route operates without ROM files.
        for version in self.versions:
            self.project.version(version).baserom.unlink()

    def ci(self, out):
        with (
            patch.dict(os.environ, {"GITHUB_SHA": "952c549792f0328a00979e449cfd571a48a6b6a6"}),
            patch.object(sys, "argv", ["verify", "--project", str(self.project.root), "--artifacts", str(out)]),
            patch("subprocess.run", side_effect=AssertionError("no native, compiler, or manual sync")),
        ):
            return verify.main()

    def test_actual_stale_projection_is_regenerated_in_canonical_ci_before_artifact_export(self):
        self.stale_projection()
        inputs = verify.source_pins(self.project)
        ledger = (self.project.root / attempts.PATH).read_bytes()
        out = self.project.root / "out"
        self.assertEqual(self.ci(out), 0)
        manifest = verify.validate(self.project)
        self.assertEqual(verify.source_pins(self.project), inputs)
        self.assertEqual((self.project.root / attempts.PATH).read_bytes(), ledger)
        self.assertEqual(self.source.read_bytes(), CURRENT)
        for version in self.versions:
            draft = manifest["versions"][version]["drafts"][0]
            self.assertIsNone(draft["score"])
            self.assertEqual(draft["source_sha256"], hashlib.sha256(CURRENT).hexdigest())
            self.assertEqual(manifest["versions"][version]["draft_weighted_bytes"], 0)
            artifact_manifest = json.loads((out / f"{version}_report.manifest.json").read_text())
            self.assertEqual(artifact_manifest["source_commit"], "952c549792f0328a00979e449cfd571a48a6b6a6")
            self.assertEqual(
                artifact_manifest["report_sha256"],
                hashlib.sha256((self.project.root / "versions" / version / "report.json").read_bytes()).hexdigest(),
            )
        # The unmodified generated workflows call this normal entrypoint, without a regeneration mode.
        github = (self.project.root / ".github/workflows/progress.yml").read_text()
        gitlab = (self.project.root / ".gitlab-ci.yml").read_text()
        command = "python3 -m unbake.report.verify --project . --artifacts out"
        self.assertIn(command, github)
        self.assertIn(command, gitlab)
        self.assertNotIn("--regenerate", github + gitlab)
        self.assertIn(hashlib.sha256((self.project.root / verify.BUNDLE).read_bytes()).hexdigest(), github)

    def test_actual_ci_regeneration_preserves_strict_current_receipts_and_exports_nothing_on_source_fault(self):
        self.stale_projection()
        # This is an actual captured source with a deliberately invalid live receipt,
        # distinct from its intact historical qualification. Regeneration must reject it.
        current = state.inventory(self.project)
        receipt = dict(current.receipts[NAME])
        receipt["source_sha256"] = hashlib.sha256(OLD).hexdigest()
        before = (self.project.root / verify.MANIFEST).read_bytes()
        real_inventory = state.inventory

        def inventory(project, *, receipts=None):
            return real_inventory(project, receipts={NAME: receipt} if receipts is None else receipts)

        out = self.project.root / "out"
        with patch.object(state, "inventory", side_effect=inventory), self.assertRaises(SystemExit) as failure:
            self.ci(out)
        self.assertEqual(failure.exception.code, 1)
        self.assertFalse(out.exists())
        self.assertEqual((self.project.root / verify.MANIFEST).read_bytes(), before)
        self.assertEqual((self.project.root / attempts.PATH).read_bytes(), self.history)
