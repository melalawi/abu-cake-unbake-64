"""Regressions for explicit proof, bulk landing, and host queue locks."""

from __future__ import annotations

import argparse
import json
from dataclasses import replace
from unittest.mock import patch

from tests.match.support import MatchFixture
from unbake.cli import submit as cli_submit
from unbake.decomp import drafts, trial
from unbake.match import common, free
from unbake.match import queue as match
from unbake.project.clone import isolated_policy
from unbake.project.config import Held


class MatchFixTests(MatchFixture):
    def test_submit_refuses_arbitrary_source_name_without_rewriting_it(self) -> None:
        original = self.draft("alpha")
        source = self.sources / "draft-variant.input"
        source.write_bytes(original.read_bytes())
        before = source.read_bytes()
        with self.assertRaises(Held):
            match.submit(self.project, self.policy, source)
        self.assertEqual(self.queued(), [])
        self.assertEqual(source.read_bytes(), before)

    def test_submit_uses_selected_trial_policy_even_when_clone_policy_exists(self) -> None:
        source = self.draft("alpha")
        self.project.tools.joinpath("clone-policy.toml").write_text(isolated_policy(self.policy, self.root))
        with patch("unbake.project.config.load_policy", side_effect=AssertionError("policy changed")):
            match.submit(self.project, self.policy, source)
        self.assertEqual([row["function"] for row in self.queued()], ["alpha"])

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
            retained = source
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
        retained = source
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

    def test_cli_current_exact_try_satisfies_submit_with_publication_wrapper(self) -> None:
        import io
        from contextlib import nullcontext, redirect_stderr, redirect_stdout

        from unbake.cli.main import main
        from unbake.decomp.trial_compare import Compare

        for version in self.versions:
            path = self.project.version(version).split
            path.write_text(
                path.read_text().replace("    start: 0x1000\n", "    start: 0x1000\n    vram: 0x80001000\n")
            )
        source = self.sources / "alpha.c"
        plain = b'#include "types.h"\n\nint alpha(void) { return 0; }\n'
        source.write_bytes(b'#include "types.h"\n#ifdef NON_MATCHING\nint alpha(void) { return 0; }\n#endif\n')
        # Use exactly the canonical bytes preserved by the wrapper removal.
        digest = drafts.source_identity(source.read_bytes())
        result = trial.Trial(
            "alpha",
            digest,
            {
                version: Compare(
                    version,
                    4,
                    4,
                    dict.fromkeys(
                        ("register", "order", "immediate", "relocation", "inserted", "missing", "changed"), 0
                    ),
                    [],
                    match_percent=100,
                    register_changes=(),
                )
                for version in self.versions
            },
            [],
            "unbake submit alpha.c",
        )
        from unbake.decomp.work import identity as original_identity

        output = io.StringIO()
        with (
            patch("unbake.cli.main.config.load", return_value=self.project),
            patch("unbake.cli.main.config.load_policy", return_value=self.policy),
            patch.object(trial, "trial_inputs", return_value=nullcontext({})),
            patch(
                "unbake.decomp.work.identity",
                side_effect=lambda project, source, versions, **kw: original_identity(
                    project, source, versions, policy=kw.get("policy")
                ),
            ),
            patch.object(trial, "try_draft", return_value=result) as tried,
            redirect_stdout(output),
            redirect_stderr(output),
        ):
            self.assertEqual(
                main(
                    [
                        "--project",
                        str(self.root),
                        "try",
                        str(source),
                    ]
                ),
                0,
                output.getvalue(),
            )
            match.submit(self.project, self.policy, source)
            self.assertEqual(tried.call_count, 1)
        row = self.queued()[0]
        self.assertEqual(row["source_sha256"], digest)
        self.assertEqual(row["source"], str(source))
        self.assertIn(b"NON_MATCHING", source.read_bytes())
        source.write_bytes(plain.replace(b"return 0", b"return 1"))
        with self.assertRaisesRegex(Held, "trial.source_sha256"):
            match.submit(self.project, self.policy, source)
