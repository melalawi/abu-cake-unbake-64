"""Regressions for automatic proof, bulk landing, and host queue locks."""

from __future__ import annotations

import argparse
from pathlib import Path
from unittest.mock import patch

from tests.match.support import MatchFixture
from unbake.match import common, free, proof
from unbake.match import queue as match
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
            retained = proof.source(self.project, self.policy, source)
            self.prove(retained, identical=identical, versions=["us"])
        receipts = free.land(self.project, self.policy, "us")
        self.assertEqual({row["function"] for row in self.matched()}, {"alpha", "beta"})
        self.assertTrue(any("skipped gamma" in line for line in receipts))
        self.assertEqual(self.calls, [("alpha", "beta")])
        self.assertIn("#ifdef NON_MATCHING", (self.src / "gamma.c").read_text())
        self.assertEqual(self.current(self.project, "eu"), self.original["eu"])
        self.assertNotIn(", c, ", self.project.version("eu").split.read_text())
        self.assertEqual(self.queued(), [])

    def test_queue_lock_lives_in_policy_state_and_is_shared_by_queue_operations(self) -> None:
        source = self.draft("alpha")
        match.submit(self.project, self.policy, source)
        match.status(project=self.project, policy=self.policy)
        match.withdraw("alpha", project=self.project, policy=self.policy)
        with common.queue_lock(self.project, self.policy):
            locks = list((self.policy.state_root / self.project.name / "locks").glob("*.lock"))
            self.assertEqual(len(locks), 1)
        self.assertFalse((self.root / "data" / "match-queue.lock").exists())
