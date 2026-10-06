"""Guarded publication reuses exact land's writer while retaining ROM ownership and measured history."""

import argparse
import hashlib
import io
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tests.project_fixture import ProjectCase
from unbake import buildfiles, config, land, runner
from unbake.cli import publish
from unbake.cli.args import Context
from unbake.config import Held
from unbake.fold.apply import Folded
from unbake.report import progress
from unbake.work import attempts, draft

SOURCE = "int alpha(void) { return 1; }\n"


class FuzzyPublishTests(ProjectCase):
    def setUp(self):
        super().setUp()
        self.file = self.project.work / "alpha/alpha.c"
        self.file.parent.mkdir(parents=True)
        self.file.write_text(SOURCE)
        self.splits = {v: self.project.version(v).split.read_bytes() for v in self.versions}

    def publish_source(self, score, *, fuzzy=True, fail_commit=False, cli=False):
        scores = {v: {"compiled": True, "percent": score, "exact": False} for v in self.versions}
        stream = io.StringIO()
        records = []
        prove = land.Proof(list(self.versions), set(), scores if fuzzy else None)
        with (
            patch("unbake.fold.apply.fold", return_value=Folded("alpha", self.file.read_text(), {}, ())),
            patch("unbake.fold.apply.private_headers", return_value={}),
            patch.object(land, "prove", return_value=prove),
            patch.object(land, "exact_attempt", return_value=SimpleNamespace(compiler="ido-7.1", sha256="a" * 64)),
            patch.object(land, "_commit", side_effect=Held("land", "hook refused") if fail_commit else None) as commit,
            patch.object(
                land,
                "_git",
                side_effect=lambda project, *args: attempts.guarded(SOURCE) if args[0] == "show" else "c0ffee\n",
            ),
            patch.object(buildfiles, "write", return_value=[]),
            patch.object(land.steps, "record"),
            patch.object(land.steps, "ensure") as ensure,
            patch.object(progress, "write", return_value=[]),
        ):
            if cli:
                parser = argparse.ArgumentParser()
                publish.register(parser)
                args = parser.parse_args(["--fuzzy", "--events", str(self.file)])
                result = publish.run(Context("publish", args, self.project.root, None, stream, self.host))
                ensure.assert_not_called()
                return result, stream.getvalue()
            result = land.land(self.project, self.host, self.file, fuzzy=fuzzy, on_commit=records.append)
        return result, records, commit.call_args

    def test_low_score_is_committed_guarded_with_original_rom_rows_and_exact_progress(self):
        before = progress.measure(self.project, self.host, "us")["measures"]
        result, records, commit = self.publish_source(1.0)
        self.assertEqual(result, "c0ffee")
        source = self.project.src / "alpha.c"
        self.assertEqual(source.read_text(), attempts.guarded(SOURCE))
        self.assertEqual({v: self.project.version(v).split.read_bytes() for v in self.versions}, self.splits)
        self.assertIn(attempts.summary_path(self.project), commit.args[2])
        self.assertEqual(commit.args[3], "Fuzzy alpha")
        receipt = attempts.fuzzy(self.project, "alpha")
        self.assertEqual(receipt["source_sha256"], hashlib.sha256(source.read_bytes()).hexdigest())
        self.assertEqual(records[0]["proof"]["versions"], list(self.versions))
        self.assertEqual(draft.published_seed(self.project, "alpha"), SOURCE)
        self.assertTrue(self.file.exists())
        after = progress.measure(self.project, self.host, "us")["measures"]
        self.assertEqual(after["matched_code"], before["matched_code"])
        self.assertAlmostEqual(after["fuzzy_match_percent"], 1.0 / 3, places=6)
        self.assertEqual(buildfiles.units(self.project, "us"), [])
        self.assertIn("-DNON_MATCHING", buildfiles.units_mk(self.project))
        self.assertNotIn("alpha.bin", buildfiles.slices_mk(self.project, "us"))

    def test_first_unknown_measurement_is_explicit_and_never_replaces(self):
        self.publish_source(None)
        self.assertIsNone(attempts.fuzzy(self.project, "alpha")["score"])
        self.assertEqual(attempts.summaries(self.project)["alpha"].best, {})
        with self.assertRaisesRegex(Held, "strictly higher measured score"):
            self.publish_source(None)

    def test_only_strictly_improved_measured_draft_replaces(self):
        self.publish_source(20.0)
        before = attempts.summary_path(self.project).read_bytes()
        for score in (None, 19.0, 20.0):
            with self.subTest(score=score), self.assertRaisesRegex(Held, "strictly higher measured score"):
                self.publish_source(score)
            self.assertEqual(attempts.summary_path(self.project).read_bytes(), before)
        self.file.write_text("int alpha(void) { return 2; }\n")
        self.publish_source(21.0)
        self.assertEqual(attempts.fuzzy(self.project, "alpha")["score"], 21.0)
        self.assertIn("return 2", (self.project.src / "alpha.c").read_text())

    def test_exact_land_promotes_guarded_source_and_clears_receipt(self):
        self.publish_source(20.0)
        self.publish_source(100.0, fuzzy=False)
        self.assertIsNone(attempts.fuzzy(self.project, "alpha"))
        self.assertEqual((self.project.src / "alpha.c").read_text(), SOURCE)
        self.assertNotIn("-DNON_MATCHING", buildfiles.units_mk(config.load(self.project.root)))
        for version in self.versions:
            self.assertIn("c, alpha]", self.project.version(version).split.read_text())
        self.file.parent.mkdir(parents=True)
        self.file.write_text(SOURCE)
        with self.assertRaisesRegex(Held, "cannot replace exact C"):
            self.publish_source(99.0)

    def test_commit_failure_restores_source_and_receipt(self):
        self.publish_source(20.0)
        before = attempts.summary_path(self.project).read_bytes()
        source = (self.project.src / "alpha.c").read_bytes()
        self.file.write_text("int alpha(void) { return 2; }\n")
        with self.assertRaisesRegex(Held, "hook refused"):
            self.publish_source(30.0, fail_commit=True)
        self.assertEqual(attempts.summary_path(self.project).read_bytes(), before)
        self.assertEqual((self.project.src / "alpha.c").read_bytes(), source)

    def test_source_rules_refuse_even_with_an_exception_marker(self):
        self.file.write_text('/* FAKEMATCH: test */\nint alpha(void) { asm("nop"); return 1; }\n')
        with self.assertRaisesRegex(Held, "land.fuzzy_rules"):
            self.publish_source(99.0)
        self.assertFalse((self.project.src / "alpha.c").exists())

    def test_cli_returns_holding_versions_and_immediate_fuzzy_receipt(self):
        result, stream = self.publish_source(5.0, cli=True)
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.data["versions"]["alpha"], list(self.versions))
        self.assertEqual(result.data["fuzzy"]["alpha"]["kind"], "fuzzy")
        self.assertIn('"event":"fn.committed"', stream.replace(" ", ""))
        for kwargs in ({"originals": ("alpha",)}, {"required_versions": ("us",)}):
            with self.assertRaisesRegex(Held, "publish.fuzzy"):
                land.publish(self.project, self.host, [self.file], fuzzy=True, **kwargs)

    def test_compilation_is_required_in_every_holding_version_but_measurement_can_be_unavailable(self):
        jobs = []

        def run(host, fn, items):
            jobs.extend(item[-1] for item in items)
            return [({"compiled": v == "us", "percent": None}, set()) for v in self.versions]

        with (
            patch("unbake.pool.run", side_effect=run),
            self.assertRaisesRegex(Held, "1 holding versions refused") as failure,
        ):
            land.prove(self.project, self.host, "alpha", attempts.guarded(SOURCE), {}, self.root / "stage", fuzzy=True)
        self.assertEqual(jobs, list(self.versions))
        self.assertEqual(failure.exception.failures[0]["version"], "eu")
        with (
            patch.object(runner, "compile_unit", return_value=nullcontext(Path("alpha.o"))),
            patch("unbake.compilers.fingerprint._body", return_value=b"body"),
            patch.object(runner, "dependencies", return_value=set()),
            patch.object(runner, "link_function", side_effect=Held("link", "unavailable")),
        ):
            measured, _ = land._fuzzy_builds_row((self.project, self.project, self.host, "alpha", self.file, "us"))
        self.assertTrue(measured["compiled"])
        self.assertIsNone(measured["percent"])
        self.assertIn("fault", measured)

    def test_definition_must_have_the_canonical_abi(self):
        with (
            patch("unbake.decomp.draft_abi.mapped_body", return_value=object()),
            patch(
                "unbake.typemap.types_db.entries",
                return_value={"alpha": {"state": "known", "prototype": "int alpha(void);"}},
            ),
            patch("unbake.typemap.types_db.meta", return_value={}),
        ):
            land._fuzzy_signature(self.project, "alpha", "us", SOURCE)
            land._fuzzy_signature(self.project, "alpha", "us", "typedef signed int s32; s32 alpha(void) { return 1; }")
            with self.assertRaisesRegex(Held, "undeclared missing"):
                land._fuzzy_signature(self.project, "alpha", "us", "int alpha(void) { return missing(); }")
            with self.assertRaisesRegex(Held, "definition differs from canonical"):
                land._fuzzy_signature(self.project, "alpha", "us", "int alpha(int value) { return value; }")

    def test_replacement_score_weights_versions_by_target_size(self):
        with patch.object(
            land.compare, "row_of", side_effect=lambda p, f, v: SimpleNamespace(start=0, end=12 if v == "us" else 36)
        ):
            self.assertEqual(
                land._fuzzy_score(self.project, "alpha", {"us": {"percent": 10.0}, "eu": {"percent": 90.0}}), 70.0
            )

    def test_progress_keeps_retained_alias_receipt_and_weights_the_owning_row(self):
        self.publish_source(50.0)
        path = self.project.version("us").split
        path.write_text(path.read_text().replace("asm, alpha]", "asm, alpha_row]"))
        current = config.load(self.project.root)
        with (
            patch.object(progress, "readme_descriptions", return_value={v: v for v in self.versions}),
            patch.object(progress, "render", return_value="progress"),
        ):
            progress.write(current, self.host)
        self.assertIsNotNone(attempts.fuzzy(current, "alpha"))
        report = progress.measure(current, self.host, "us")
        row = next(unit for unit in report["units"] if unit["name"] == "alpha_row")
        self.assertEqual(row["metadata"]["source_path"], "src/alpha.c")
        self.assertFalse(row["metadata"]["complete"])
        self.assertAlmostEqual(report["measures"]["fuzzy_match_percent"], 50 / 3, places=5)

    def test_implicit_calls_and_decompiler_placeholders_are_not_admitted(self):
        with self.assertRaisesRegex(Held, "undeclared missing"):
            land._fuzzy_calls("alpha", "int alpha(void) { return missing(); }")
        with self.assertRaisesRegex(Held, "unresolved M2C_ERROR"):
            land._fuzzy_calls("alpha", "int M2C_ERROR(void); int alpha(void) { return M2C_ERROR(); }")
        with self.assertRaisesRegex(Held, "unresolved M2C_UNK"):
            land._fuzzy_calls("alpha", "typedef int M2C_UNK; int alpha(void) { M2C_UNK value = 1; return value; }")
        land._fuzzy_calls("alpha", "int supplied(void); int alpha(void) { return supplied(); }")
        land._fuzzy_calls("alpha", "int alpha(int (*callback)(void)) { return callback(); }")
        land._fuzzy_calls(
            "alpha", "int supplied(void); int alpha(void) { int (*callback)(void) = supplied; return callback(); }"
        )
