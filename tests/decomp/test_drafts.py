"""Draft history, ranking, publication, and refused incomplete trial records."""

import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

from unbake.decomp.drafts import Store
from unbake.project.config import Held, Policy, Project


class DraftsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir=os.environ["TMPDIR"])
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.project: Any = SimpleNamespace(
            root=self.root / "project", name="fixture", src=self.root / "project/src", version=self.version
        )
        self.policy: Any = SimpleNamespace(state_root=self.root / "state")
        self.store = Store(cast(Policy, self.policy), cast(Project, self.project))
        self.project.src.mkdir(parents=True)

    def version(self, name: str) -> SimpleNamespace:
        if name not in ("us", "eu"):
            raise Held("config", f"version.{name} is missing")
        return SimpleNamespace(name=name)

    def add(
        self,
        content: str,
        scores: dict[str, float],
        identical: tuple[int, int] = (1, 1),
        *,
        function: str = "sample",
        typed: dict[str, int] | None = None,
    ) -> tuple[str, Any, Path]:
        source = self.root / f"{function}.c"
        source.write_text(content)
        differences = dict.fromkeys(
            ("register", "order", "immediate", "relocation", "inserted", "missing", "changed"), 0
        )
        if typed:
            differences.update(typed)
        compares = {
            v: SimpleNamespace(version=v, identical=count, of=10, typed=differences.copy(), lines=[])
            for v, count in zip(("us", "eu"), identical, strict=False)
        }
        trial: Any = SimpleNamespace(
            function=function,
            source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
            compares=compares,
            preconditions=[],
            next_command="unbake match submit sample.c",
            identical_everywhere=all(x == 10 for x in identical)
            and not any(differences.values())
            and all(value == 100 for value in scores.values()),
        )
        return self.store.add(trial, source, scores), trial, source

    def test_rank_uses_exact_words_across_all_versions_before_objdiff(self) -> None:
        sha, _, _ = self.add("unbalanced", {"us": 100, "eu": 80}, (10, 10))
        self.add("balanced", {"us": 90, "eu": 90}, (1, 1))
        self.add("fewer identical", {"us": 90, "eu": 91}, (3, 4))
        self.assertEqual(self.store.best("sample"), self.store.root / sha / "sample.c")
        selected = self.store.best("sample")
        assert selected is not None
        self.assertEqual(selected.read_text(), "unbalanced")

    def test_rank_prefers_complete_identity_before_more_words(self) -> None:
        sha, _, _ = self.add("complete", {"us": 100, "eu": 100}, (10, 10))
        self.add("extra instruction", {"us": 100, "eu": 100}, (10, 10), typed={"inserted": 1})
        self.assertEqual(self.store.best("sample"), self.store.root / sha / "sample.c")

    def test_fewer_typed_differences_precede_objdiff_tiebreak(self) -> None:
        self.add("extra instruction", {"us": 99, "eu": 99}, (9, 9), typed={"inserted": 1, "changed": 1})
        sha, _, _ = self.add("fewer differences", {"us": 97, "eu": 98}, (9, 9), typed={"changed": 1})
        self.assertEqual(self.store.best("sample"), self.store.root / sha / "sample.c")
        self.assertEqual(self.store.publish_all()[0].read_text(), "#ifdef NON_MATCHING\nfewer differences\n#endif\n")

    def test_publish_rejects_higher_objdiff_extra_instruction_variant(self) -> None:
        self.add("extra instruction", {"us": 99, "eu": 99}, (8, 9), typed={"inserted": 1})
        sha, _, _ = self.add("more exact words", {"us": 97, "eu": 96}, (10, 8), typed={"changed": 2})
        self.assertEqual(self.store.best("sample"), self.store.root / sha / "sample.c")
        self.assertEqual(self.store.publish_all()[0].read_text(), "#ifdef NON_MATCHING\nmore exact words\n#endif\n")

    def test_latest_trial_per_sha_and_all_history_survive_reopening(self) -> None:
        sha, trial, source = self.add("same draft", {"us": 95, "eu": 95})
        winner, _, _ = self.add("other draft", {"us": 90, "eu": 90})
        source.write_text("same draft")
        self.store.add(trial, source, {"us": 85, "eu": 85})
        reopened = Store(self.policy, self.project)
        self.assertEqual(len(reopened.history()), 3)
        self.assertEqual(reopened.best("sample"), reopened.root / winner / "sample.c")
        self.assertEqual(len(list(reopened.root.glob("*/*.c"))), 2)
        self.assertEqual(reopened.rows("sample")[0]["source_sha256"], sha)

    def test_publish_guards_best_draft_and_skips_matched_source(self) -> None:
        self.add("perfect draft", {"us": 100, "eu": 100}, (10, 10))
        self.add("already matched", {"us": 100, "eu": 100}, (10, 10), function="matched")
        (self.project.src / "matched.c").write_text("already matched")
        paths = self.store.publish_all()
        self.assertEqual(paths, [self.project.src / "sample.c"])
        self.assertEqual(paths[0].read_text(), "#ifdef NON_MATCHING\nperfect draft\n#endif\n")
        self.assertEqual((self.project.src / "matched.c").read_text(), "already matched")
        self.assertFalse((self.project.root / "nonmatching").exists())
        self.assertTrue(self.store.rows("sample")[0]["identical_everywhere"])

    def test_added_words_prevent_identical_everywhere(self) -> None:
        _, _, _ = self.add("longer", {"us": 100, "eu": 100}, (10, 10), typed={"inserted": 1})
        self.assertFalse(self.store.rows("sample")[0]["identical_everywhere"])

    def test_source_digest_score_versions_and_missing_fields_are_refused(self) -> None:
        _, trial, source = self.add("draft", {"us": 50, "eu": 60})
        source.write_text("changed")
        with self.assertRaisesRegex(Held, "source_sha256"):
            self.store.add(trial, source, {"us": 50, "eu": 60})
        source.write_text("draft")
        with self.assertRaisesRegex(Held, "score VERSIONs"):
            self.store.add(trial, source, {"us": 50})
        del trial.next_command
        with self.assertRaisesRegex(Held, "next_command"):
            self.store.add(trial, source, {"us": 50, "eu": 60})
        self.assertEqual(len(self.store.rows("sample")), 1)

    def test_invalid_scores_and_function_paths_are_refused(self) -> None:
        for value in (float("nan"), float("inf"), -1, 101, True):
            with self.subTest(value=value), self.assertRaisesRegex(Held, "score"):
                self.add("draft", {"us": value, "eu": 50})
        with self.assertRaisesRegex(Held, "function"):
            self.store.rows("../sample")

    def test_corrupt_ledger_and_content_are_named(self) -> None:
        sha, _, _ = self.add("draft", {"us": 50, "eu": 50})
        (self.store.root / sha / "sample.c").write_text("tampered")
        with self.assertRaisesRegex(Held, sha):
            self.store.best("sample")
        (self.store.root / "trials.jsonl").write_text('{"function":"sample"}\n')
        with self.assertRaisesRegex(Held, "source_sha256"):
            self.store.rows("sample")

    def test_empty_store_does_not_create_state(self) -> None:
        self.assertEqual(self.store.rows("sample"), [])
        self.assertIsNone(self.store.best("sample"))
        self.assertEqual(self.store.publish_all(), [])
        self.assertFalse(self.store.root.exists())
