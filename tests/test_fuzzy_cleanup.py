"""Real BattleTanx cleanup must retain an equal score and close the admission gap."""

import hashlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tests.project_fixture import ProjectCase
from unbake import build, buildfiles, config, land
from unbake.config import Held
from unbake.decomp import checks
from unbake.fold.apply import Folded
from unbake.report import progress
from unbake.work import attempts

FUNCTION = "func_80106010_us"
FIXTURES = Path(__file__).parent / "fixtures/battletanx_fuzzy_cleanup"
COMMITTED = (FIXTURES / "committed.c").read_text()
CLEANED = (FIXTURES / "cleaned.c").read_text()
SIMPLE = f"int {FUNCTION}(void) {{ return 1; }}\n"


class FuzzyCleanupTests(ProjectCase):
    def setUp(self):
        super().setUp()
        # Give the tiny ROM fixture the real entry name without changing either
        # source in the owner-provided pair.
        for path in [
            self.project.root / "layout.toml",
            *self.project.root.glob("versions/*/*.yaml"),
            *self.project.root.glob("versions/*/symbol_addrs.txt"),
        ]:
            path.write_text(path.read_text().replace("alpha", FUNCTION))
        self.project = config.load(self.project.root)
        self.file = self.project.work / FUNCTION / f"{FUNCTION}.c"
        self.file.parent.mkdir(parents=True)
        self.source = self.project.src / f"{FUNCTION}.c"
        self.committed = COMMITTED

    def seed(self, source=COMMITTED, score=20.0):
        self.committed = source if source.startswith(attempts.FUZZY_PREFIX) else attempts.guarded(source)
        self.source.write_text(self.committed)
        receipt = {
            "source_sha256": hashlib.sha256(self.committed.encode()).hexdigest(),
            "compiler": "ido-7.1",
            "score": score,
            "versions": {v: score for v in self.versions},
        }
        attempts.summary_path(self.project).write_bytes(
            attempts.encode({FUNCTION: attempts.Summary(12, {}, False, 0.0, 1, receipt)})
        )

    def publish_source(self, source=CLEANED, score=20.0, *, fuzzy=True, folded=None):
        self.file.write_text(source)
        scores = {v: {"compiled": True, "percent": score, "exact": not fuzzy} for v in self.versions}
        with (
            patch(
                "unbake.fold.apply.fold", return_value=Folded(FUNCTION, source if folded is None else folded, {}, ())
            ),
            patch("unbake.fold.apply.private_headers", return_value={}),
            patch.object(land, "prove", return_value=land.Proof(list(self.versions), set(), scores if fuzzy else None)),
            patch.object(land, "exact_attempt", return_value=SimpleNamespace(compiler="ido-7.1")),
            patch.object(
                land, "_git", side_effect=lambda p, *args: self.committed if args[0] == "show" else "c0ffee\n"
            ),
            patch.object(land, "_commit") as commit,
            patch.object(buildfiles, "write", return_value=[]),
            patch.object(land.steps, "record"),
            patch.object(land.steps, "ensure"),
            patch.object(progress, "write", return_value=[]),
        ):
            result = land.land(self.project, self.host, self.file, fuzzy=fuzzy)
        return result, commit.call_args

    def test_cleanup_cannot_introduce_any_new_violation(self):
        for score in (20.0, 21.0):
            for violation in ("#define LOCAL_VALUE 1", "int M2C_ERROR(void);", "volatile int extra;"):
                with self.subTest(score=score, violation=violation):
                    self.seed()
                    with self.assertRaisesRegex(Held, "land.fuzzy_rules"):
                        self.publish_source(violation + "\n" + CLEANED, score=score)
                    self.assertEqual(self.source.read_text(), COMMITTED)

    def test_real_raw_source_cannot_be_admitted_as_fuzzy(self):
        with self.assertRaisesRegex(Held, "land.fuzzy_rules.*raw display list"):
            self.publish_source(attempts.unguarded(COMMITTED))
        self.assertFalse(self.source.exists())

    def test_exact_publish_checks_final_folded_source(self):
        with self.assertRaisesRegex(Held, "land.rules.*raw display list"):
            self.publish_source(fuzzy=False, folded=attempts.unguarded(COMMITTED))
        self.assertFalse(self.source.exists())

    def test_exact_attempt_rechecks_current_rules_despite_old_exact_receipt(self):
        self.file.write_text(COMMITTED)
        measured = attempts.Attempt(
            "t",
            FUNCTION,
            hashlib.sha256(self.file.read_bytes()).hexdigest(),
            12,
            {v: {"percent": 100.0, "exact": True} for v in self.versions},
            100.0,
            True,
            0.1,
            "ido-7.1",
        )
        with patch.object(attempts, "read", return_value=[measured]):
            with self.assertRaisesRegex(Held, "land.rules.*raw display list"):
                land.exact_attempt(self.project, FUNCTION, self.file)
            with self.assertRaisesRegex(Held, "land.rules.*raw display list"):
                land.exact_attempt(self.project, FUNCTION, self.file, required_versions=self.versions)

    def test_shared_check_sees_real_raw_source_but_accepts_cleaned_pair(self):
        self.assertTrue(checks.unmarked(COMMITTED))
        self.assertEqual({finding.rule for finding in checks.unmarked(COMMITTED)}, {"raw-gfx"})
        self.assertEqual(checks.run(CLEANED), [])
        self.source.write_text(COMMITTED)
        self.assertTrue(build.source_findings(self.project))
        self.source.write_text(attempts.guarded(CLEANED))
        self.assertEqual(build.source_findings(self.project), [])
