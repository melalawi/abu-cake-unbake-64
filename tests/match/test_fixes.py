"""Regressions for automatic proof, bulk landing, and host queue locks."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from unittest.mock import patch

from tests.match.support import MatchFixture
from unbake.cli import match as cli_match
from unbake.decomp import drafts
from unbake.match import common, free, proof
from unbake.match import queue as match
from unbake.project.clone import isolated_policy
from unbake.project.config import Policy, Project


class MatchFixTests(MatchFixture):
    def test_submit_runs_trial_only_when_exact_bytes_have_no_record(self) -> None:
        for partial in (False, True):
            with self.subTest(partial=partial):
                source = self.sources / "alpha.c"
                text = f"int alpha(void) {{ return {int(partial)}; }}\n"
                source.write_text("#ifdef NON_MATCHING\n" + text + "#endif\n" if partial else text)

                def retain(
                    args: argparse.Namespace,
                    project: Project,
                    policy: Policy,
                    candidate: Path,
                    versions: list[str],
                    expected: str = text,
                ) -> None:
                    self.assertEqual(candidate.read_text(), expected)
                    self.assertEqual(versions, list(self.versions))
                    self.assertFalse(args.scratch.is_relative_to(self.root))
                    self.prove(candidate)

                with patch.object(proof, "trial", side_effect=retain) as tried:
                    match.submit(self.project, self.policy, source)
                    match.submit(self.project, self.policy, source)
                    self.assertEqual(tried.call_count, 1)
                self.assertEqual(len(self.queued()), 1)

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

        def retain(
            args: argparse.Namespace, project: Project, policy: Policy, candidate: Path, versions: list[str]
        ) -> None:
            self.assertFalse(args.scratch.is_relative_to(self.root))
            self.assertEqual(policy.state_root, self.root / ".unbake" / "state")
            with patch.object(self, "store", drafts.Store(policy, project)):
                self.prove(candidate, versions=versions)

        with patch.object(proof, "trial", side_effect=retain) as tried:
            cli_match.run(argparse.Namespace(verb="submit", source=source), self.project, self.policy)
            self.assertEqual(tried.call_count, 1)
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
