"""Candidate header failures roll back and stale fuzzy evidence gets a fresh proof."""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from unbake.decomp import drafts
from unbake.layout.header_context import Headers
from unbake.layout.split import Edit
from unbake.match import batch, batch_fold, declarations, nonmatching
from unbake.project.config import Held


class BatchHeaderHoldsTests(unittest.TestCase):
    def test_candidate_failure_restores_overlay_and_fold_changes_before_next_candidate(self):
        for stage in ("overlay", "fold", "apply"):
            with self.subTest(stage=stage):
                root = Path("/p")
                texts = {root / "base.h": "struct Base {int value;};"}
                headers = Headers(texts, root=root)
                candidates = [SimpleNamespace(function=name) for name in ("bad", "good")]
                receipts = []

                def ordered(fn, shared, members, cores):
                    return iter(([], batch_fold.Trial("again")) for _ in members)

                def fold(staged, policy, headers, candidate, changes, root=root, stage=stage, texts=texts):
                    if candidate.function == "bad":
                        changes.apply(headers, [Edit(root / "overlay.h", "", "struct Overlay {int value;};", ())])
                        if stage != "overlay":
                            changes.apply(headers, [Edit(root / "fold.h", "", "struct Fold {int value;};", ())])
                        if stage == "apply":
                            changes.apply(headers, [Edit(root / "broken.h", "", "struct Broken {Missing value;};", ())])
                        raise Held("structs", f"headers.declaration: {stage}: candidate provider")
                    self.assertEqual(headers.texts, texts)
                    self.assertNotIn("struct Overlay", headers.types)
                    self.assertNotIn("struct Fold", headers.types)
                    return declarations.Folded("good", "int good(void) {return 0;}", [], {})

                with (
                    patch.object(batch_fold.forked, "ordered", side_effect=ordered),
                    patch.object(batch_fold, "_fold_one", side_effect=fold),
                ):
                    accepted = batch_fold.fold(None, SimpleNamespace(cores=1), headers, candidates, receipts)
                self.assertEqual([c.function for c, _ in accepted], ["good"])
                self.assertEqual(len(receipts), 1)
                self.assertIn("bad: submit.fold:", receipts[0])
                self.assertIn("broken.h: Broken.value" if stage == "apply" else "candidate provider", receipts[0])

    def test_stale_fuzzy_refusal_cannot_veto_current_cartridge_proof(self):
        cases = [
            (None, None, False),
            (Held("submit", "submit.flags: changed since latest try"), None, True),
            (Held("submit", "submit.target_sha256: changed since latest try"), None, True),
            (Held("submit", "trial.receipt: malformed"), None, "trial.receipt"),
            (None, Held("submit", "submit.owner_fuzzy_bar: real current failure"), "owner_fuzzy_bar"),
        ]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "alpha.c"
            source.write_text("int alpha(void) {return 0;}")
            project = SimpleNamespace(src=root / "src")
            sha = drafts.source_identity(source.read_bytes())
            inputs = SimpleNamespace(
                owners={}, trials={"alpha": [{"source_sha256": sha, "identical_everywhere": False, "work": {}}]}
            )
            for stale, refusal, expected in cases:
                with (
                    self.subTest(stale=stale, refusal=refusal),
                    patch.object(batch.split, "holding_versions", return_value=("us",)),
                    patch.object(batch.checks, "run", return_value=[]),
                    patch.object(batch.work, "current_trial", side_effect=stale),
                    patch.object(
                        nonmatching,
                        "admit",
                        return_value={"compiler_evidence": {"selected": "fixture"}},
                        side_effect=refusal,
                    ) as admit,
                ):
                    if isinstance(expected, str):
                        with self.assertRaisesRegex(Held, expected):
                            batch._admit(project, None, inputs, source)
                    else:
                        candidate = batch._admit(project, None, inputs, source)
                        self.assertEqual(candidate.matched, expected)
                        self.assertEqual(candidate.compiler, {} if expected else {"selected": "fixture"})
                    self.assertEqual(admit.call_count, int(stale is None))
