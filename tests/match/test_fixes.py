"""Regressions for explicit proof, bulk landing, and host queue locks."""

from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from tests.match.support import MatchFixture
from unbake.cli import match as cli_match
from unbake.decomp import drafts, trial
from unbake.match import common, free, proof
from unbake.match import queue as match
from unbake.project.clone import isolated_policy
from unbake.project.config import Held


class MatchFixTests(MatchFixture):
    def test_submit_infers_function_and_retains_arbitrary_source_name(self) -> None:
        original = self.draft("alpha")
        source = self.sources / "draft-variant.input"
        source.write_bytes(original.read_bytes())
        cli_match.run(argparse.Namespace(verb="submit", source=source), self.project, self.policy)
        rows = self.queued()
        self.assertEqual(rows[0]["function"], "alpha")
        retained = Path(rows[0]["source"])
        self.assertEqual(retained.name, "alpha.c")
        self.assertEqual(retained.read_bytes(), source.read_bytes())

    def test_submit_uses_selected_trial_policy_even_when_clone_policy_exists(self) -> None:
        source = self.draft("alpha")
        self.project.tools.joinpath("clone-policy.toml").write_text(isolated_policy(self.policy, self.root))
        with patch("unbake.project.config.load_policy", side_effect=AssertionError("policy changed")):
            cli_match.run(argparse.Namespace(verb="submit", source=source), self.project, self.policy)
        self.assertEqual([row["function"] for row in self.queued()], ["alpha"])

    def test_submit_refuses_missing_trial_without_running_one(self) -> None:
        source = self.sources / "alpha.c"
        source.write_text("#ifdef NON_MATCHING\nint alpha(void) { return 1; }\n#endif\n")
        with patch.object(trial, "retain_draft") as tried:
            with self.assertRaisesRegex(Held, "alpha.*trial row missing source_sha256"):
                match.submit(self.project, self.policy, source)
            tried.assert_not_called()
        self.assertEqual(self.queued(), [])

    def test_one_version_lands_all_matching_partials_and_keeps_nonmatches(self) -> None:
        for name, identical in (("alpha", True), ("beta", True), ("gamma", False)):
            source = self.src / f"{name}.c"
            source.write_text(f"#ifdef NON_MATCHING\nint {name}(void) {{ return 0; }}\n#endif\n")
            retained = proof.source(self.project, source)
            self.prove(retained, identical=identical, versions=["us"])
        receipts = free.land(self.project, self.policy, "us")
        self.assertEqual({row["function"] for row in self.matched()}, {"alpha", "beta"})
        self.assertTrue(any("skipped gamma" in line for line in receipts))
        self.assertEqual(self.calls, [("alpha", "beta")])
        self.assertIn("#ifdef NON_MATCHING", (self.src / "gamma.c").read_text())
        self.assertEqual(self.current(self.project, "eu"), self.original["eu"])
        self.assertNotIn(", c, ", self.project.version("eu").split.read_text())
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
            cli_match.run(argparse.Namespace(verb="submit", source=source), self.project, self.policy)
            tried.assert_not_called()
        rows = common.queue(self.project)
        self.assertEqual([row["function"] for row in rows], ["alpha"])
        retained = Path(rows[0]["source"])
        self.assertTrue(retained.is_relative_to(self.root / ".unbake" / "state"))
        self.assertEqual(retained.read_bytes(), original)
        self.assertEqual(rows[0]["source_sha256"], hashlib.sha256(original).hexdigest())
        self.assertTrue(common.queue_path(self.project).is_relative_to(self.root / ".unbake" / "state"))
        self.assertEqual(legacy.read_bytes(), legacy_before)
        self.assertEqual(
            before, {path.relative_to(shared): path.read_bytes() for path in shared.rglob("*") if path.is_file()}
        )
        self.assertEqual(source.read_bytes(), b"#ifdef NON_MATCHING\n" + original + b"#endif\n")
        match.withdraw("alpha", project=self.project, policy=self.policy)
        self.assertEqual(common.queue(self.project), [])
        self.assertEqual(legacy.read_bytes(), legacy_before)
