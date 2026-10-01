"""Regressions for explicit proof, bulk landing, and host queue locks."""

from __future__ import annotations

import argparse
import json
from dataclasses import replace
from unittest.mock import patch

from tests.match.support import MatchFixture
from unbake.cli import submit as cli_submit
from unbake.decomp import drafts, trial
from unbake.match import common, free, proof
from unbake.match import queue as match
from unbake.project.clone import isolated_policy
from unbake.project.config import Held


class MatchFixTests(MatchFixture):
    def test_submit_refuses_missing_trial_without_running_one(self) -> None:
        source = self.sources / "alpha.c"
        source.write_text("#ifdef NON_MATCHING\nint alpha(void) { return 1; }\n#endif\n")
        with patch.object(trial, "retain_draft") as tried:
            with self.assertRaisesRegex(Held, "trial.source_sha256"):
                match.submit(self.project, self.policy, source)
            tried.assert_not_called()
        self.assertEqual(self.queued(), [])

    def test_one_version_lands_all_matching_partials_and_keeps_nonmatches(self) -> None:
        for name, identical in (("alpha", True), ("beta", True), ("gamma", False)):
            source = self.src / f"{name}.c"
            source.write_text(f"#ifdef NON_MATCHING\nint {name}(void) {{ return 0; }}\n#endif\n")
            retained = proof.source(self.project, source)
            self.prove(retained, identical=identical)
        receipts = free.land(self.project, self.policy, "us")
        self.assertEqual({row["function"] for row in self.matched()}, {"alpha", "beta"})
        self.assertTrue(any("skipped gamma" in line for line in receipts))
        self.assertEqual(self.calls, [("alpha", "beta")])
        self.assertIn("#ifdef NON_MATCHING", (self.src / "gamma.c").read_text())
        self.assertNotEqual(self.current(self.project, "eu"), self.original["eu"])
        self.assertIn(", c, ", self.project.version("eu").split.read_text())
        self.assertEqual(self.queued(), [])

    def test_queue_lock_lives_with_queue_and_is_shared_by_queue_operations(self) -> None:
        source = self.draft("alpha")
        match.submit(self.project, self.policy, source)
        match.status(project=self.project, policy=self.policy)
        match.withdraw("alpha", project=self.project, policy=self.policy)
        with common.queue_lock(self.project):
            locks = list(common.queue_path(self.project).parent.glob("*.lock"))
            self.assertEqual(len(locks), 1)
        self.assertFalse((self.root / "data" / "match-queue.lock").exists())

    def test_clone_submission_owns_queue_sources_and_proof_without_migration(self) -> None:
        source = self.draft("alpha")
        original = source.read_bytes()
        source.write_bytes(b"#ifdef NON_MATCHING\n" + original + b"#endif\n")
        policy_path = self.project.tools / "clone-policy.toml"
        for field in (
            "objdiff_cli",
            "m2c",
            "splat",
            "mips_ld",
            "mips_objdump",
            "mips_readelf",
            "mips_as",
            "mips_objcopy",
        ):
            executable = getattr(self.policy, field)
            executable.write_text("#!/bin/sh\nexit 0\n")
            executable.chmod(0o755)
        policy_path.write_text(isolated_policy(self.policy, self.root))
        legacy = self.root / "data" / "match-queue.jsonl"
        legacy.parent.mkdir()
        legacy.write_text(json.dumps({"function": "beta", "source": str(source), "source_sha256": "a" * 64}) + "\n")
        legacy_before = legacy.read_bytes()
        shared = self.policy.state_root
        before = {path.relative_to(shared): path.read_bytes() for path in shared.rglob("*") if path.is_file()}

        clone_policy = replace(self.policy, state_root=self.root / ".unbake" / "state")
        retained = proof.source(self.project, source)
        with patch.object(self, "store", drafts.Store(clone_policy, self.project)):
            self.prove(retained)
        with patch.object(trial, "retain_draft") as tried:
            cli_submit.run(argparse.Namespace(source=source), self.project, self.policy)
            tried.assert_not_called()
        self.assertTrue((self.src / "alpha.c").is_file())
        self.assertEqual((self.src / "alpha.c").read_bytes(), original)
        self.assertEqual(common.queue(self.project), [])
        receipts = clone_policy.state_root / self.project.id / self.project.workspace_id / "receipts/match.jsonl"
        self.assertTrue(receipts.is_file())
        self.assertEqual(legacy.read_bytes(), legacy_before)
        self.assertEqual(
            before, {path.relative_to(shared): path.read_bytes() for path in shared.rglob("*") if path.is_file()}
        )
        self.assertEqual(source.read_bytes(), b"#ifdef NON_MATCHING\n" + original + b"#endif\n")
        self.assertEqual(common.queue(self.project), [])
        self.assertEqual(legacy.read_bytes(), legacy_before)
