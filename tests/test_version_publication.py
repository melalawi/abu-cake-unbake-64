"""Required-version publication cannot weaken identity, source rules or existing C proof."""

import hashlib
from contextlib import nullcontext
from unittest.mock import patch

from tests.ledger_fixture import log_attempt
from tests.project_fixture import ProjectCase
from unbake import land
from unbake.config import Held
from unbake.fold.apply import Folded
from unbake.layout import split
from unbake.process import named
from unbake.work import attempts


class VersionPublicationTests(ProjectCase):
    def test_graphics_rewrite_proof_keeps_both_modes_in_each_published_version(self):
        from unbake.decomp import gbi_proof

        measured = []

        def code(project, host, version, source, mode):
            measured.append((version, mode))
            if version == "eu":
                raise Held(
                    named(
                        "fixture.refusal",
                        "EU uses assembly, unsupported C macro configuration",
                        owner="fixture",
                        stage="compile",
                    )
                )
            return ((".text", b"same machine code", ()),)

        with patch.object(gbi_proof, "code", side_effect=code):
            with self.assertRaisesRegex(Held, "unsupported C"):
                gbi_proof.preserve(self.project, self.host, self.file, "before", "after")
            measured.clear()
            gbi_proof.preserve(self.project, self.host, self.file, "before", "after", versions=("us",))
        self.assertEqual(measured, [("us", 0), ("us", 0), ("us", 1), ("us", 1)])

    def test_header_changes_prove_only_versions_that_build_published_c(self):
        from unbake import runner
        from unbake.layout import header_step

        source = self.project.src / "alpha.c"
        source.write_text("int alpha(void) { return 1; }\n")
        path = self.project.version("us").split
        path.write_text(path.read_text().replace("asm, alpha]", "c, alpha]"))
        compiled = []

        def compile(project, host, file, version, **kwargs):
            compiled.append(version)
            if version == "eu":
                raise Held(
                    named(
                        "fixture.refusal",
                        "EU builds assembly; this C is deliberately not supported there",
                        owner="fixture",
                        stage="compile",
                    )
                )
            return nullcontext(self.root / "alpha.o")

        with (
            patch("unbake.pool.run", side_effect=lambda host, fn, jobs: [fn(job) for job in jobs]),
            patch.object(runner, "compile_unit", side_effect=compile),
            patch.object(runner, "place"),
            patch.object(runner, "link", side_effect=lambda p, h, o, v, row, w, f: split.words(p, row)),
        ):
            self.assertEqual(
                header_step.validate(
                    self.project,
                    self.host,
                    {source: source.read_bytes()},
                    {source: {"external": ("int external();", "int external(void);")}},
                ),
                ["alpha"],
            )
        self.assertEqual(compiled, ["us", "us"])

    def test_completing_publication_invalidates_merge_eligibility_cache(self):
        from unbake.layout import merge_units

        before = merge_units.input_key(self.project)
        path = self.project.version("us").split
        path.write_text(path.read_text().replace("asm, alpha]", "c, alpha]"))
        self.assertNotEqual(merge_units.input_key(self.project), before)

    def test_compiler_choice_prioritizes_required_scope_but_never_regresses_existing_c(self):
        from unbake.compilers import choice
        from unbake.work import compare, score

        def measured(project, host, file, **kwargs):
            own = project.compiler_reference("alpha")
            versions = {}
            for v in self.versions:
                target = bytes(range(12))
                exact = (own == "ido-7.1") == (v == "eu")
                versions[v] = score.measure_words(v, target, target if exact else bytes(12))
            return compare.Compared(
                "alpha", file, hashlib.sha256(file.read_bytes()).hexdigest(), versions, compiler=own
            )

        with (
            patch.object(compare, "measure", side_effect=measured),
            patch.object(choice, "alternatives", return_value=["ido-5.3"]),
        ):
            result = compare.compare(self.project, self.host, self.file, required_versions=("us",))
            self.assertEqual(result.compiler, "ido-5.3")
            self.assertTrue(result.required_exact)
            self.assertFalse(result.exact)
            path = self.project.version("eu").split
            path.write_text(path.read_text().replace("asm, alpha]", "c, alpha]"))
            result = compare.compare(self.project, self.host, self.file, required_versions=("us",))
            self.assertEqual(result.required_versions, ("us", "eu"))
            self.assertFalse(result.required_exact)

    def test_scoped_compare_retains_optional_link_fault_without_claiming_all_version_exactness(self):
        from unbake import runner
        from unbake.compilers import choice
        from unbake.work import compare

        refusal = Held(
            named(
                "link.undefined", "link.undefined: eu has no identity for named_target", owner="fixture", stage="link"
            )
        )

        def link(project, host, obj, version, row, file):
            if version == "eu":
                raise refusal
            return split.words(project, row), []

        with (
            patch.object(runner, "compile_unit", side_effect=lambda *a, **kw: nullcontext(self.root / "alpha.o")),
            patch.object(runner, "link_function", side_effect=link),
            patch.object(choice, "alternatives", return_value=[]),
        ):
            with self.assertRaisesRegex(Held, "link.undefined"):
                compare.compare(self.project, self.host, self.file)
            result = compare.compare(self.project, self.host, self.file, required_versions=("us",))
        self.assertTrue(result.required_exact)
        self.assertFalse(result.exact)
        self.assertFalse(attempts.ledger(self.project).history("alpha")[-1].exact)
        self.assertEqual(result.document()["required_versions"], ["us"])
        self.assertIn("link.undefined", str(result.document()["versions"]["eu"]["fault"]))
        self.assertEqual(
            land.publication_versions(
                self.project, "alpha", attempts.ledger(self.project).history("alpha")[-1], ("us",)
            ),
            ("us",),
        )

    def test_scoped_write_uses_fresh_native_proof_and_keeps_other_rows_and_source_on_failure(self):
        from unbake import runner

        self.record({"us": {"percent": 100, "exact": True}, "eu": {"percent": 50, "exact": False}})
        before = {v: self.project.version(v).split.read_bytes() for v in self.versions}
        source = self.file.read_text()
        native = []
        mismatch = True

        def linked(project, host, obj, version, row, work, file):
            native.append(version)
            return bytes(row.end - row.start) if mismatch else split.words(project, row)

        with (
            patch("unbake.fold.apply.fold", return_value=Folded("alpha", source, {}, ())),
            patch("unbake.pool.run", side_effect=lambda host, fn, jobs: [fn(job) for job in jobs]),
            patch.object(runner, "compile_unit", side_effect=lambda *a, **kw: nullcontext(self.root / "alpha.o")),
            patch.object(runner, "place"),
            patch.object(runner, "link", side_effect=linked),
            patch.object(runner, "dependencies", return_value=set()),
            patch.object(land, "_commit"),
            patch.object(land, "_git", return_value="committed\n"),
            patch.object(land.buildfiles, "write", return_value=[]),
            patch.object(land.steps, "record"),
            patch("unbake.report.progress.write", return_value=[]),
        ):
            with self.assertRaisesRegex(Held, "land.mismatch"):
                land.land(self.project, self.host, self.file, required_versions=("us",))
            self.assertEqual({v: self.project.version(v).split.read_bytes() for v in self.versions}, before)
            self.assertFalse((self.project.src / "alpha.c").exists())
            mismatch = False
            land.land(self.project, self.host, self.file, required_versions=("us",))
        self.assertEqual(native, ["us", "us"])
        self.assertEqual(self.project.version("eu").split.read_bytes(), before["eu"])
        self.assertIn("c, alpha]", self.project.version("us").split.read_text())
        self.assertEqual((self.project.src / "alpha.c").read_text(), source)

    def test_partial_publication_cannot_merge_and_retire_other_version_assembly(self):
        from unbake.layout import merge_units

        owners = merge_units._owners(self.project)
        for version in self.project.versions:
            path = self.project.version(version).split
            text = path.read_text().replace("asm, beta]", "c, beta]")
            if version == "us":
                text = text.replace("asm, alpha]", "c, alpha]")
            path.write_text(text)
        owners = merge_units._owners(self.project)
        self.assertFalse(merge_units._joins(self.project, owners, "alpha", "beta"))

    def setUp(self):
        super().setUp()
        self.file = self.project.work / "alpha/alpha.c"
        self.file.parent.mkdir(parents=True)
        self.file.write_text("int alpha(void) { return 1; }\n")

    def record(self, versions):
        row = attempts.Attempt(
            "t",
            "alpha",
            hashlib.sha256(self.file.read_bytes()).hexdigest(),
            12,
            versions,
            min(v["percent"] for v in versions.values()),
            all(v["exact"] for v in versions.values()),
            0.1,
            "ido-7.1",
        )
        log_attempt(self.project, row)
        return row

    def test_required_exact_other_mismatch_is_explicit_and_default_still_refuses(self):
        row = self.record({"us": {"percent": 100, "exact": True}, "eu": {"percent": 50, "exact": False}})
        with self.assertRaisesRegex(Held, "land.not_exact"):
            land.exact_attempt(self.project, "alpha", self.file)
        self.assertEqual(land.exact_attempt(self.project, "alpha", self.file, required_versions=("us",)), row)
        self.assertEqual(land.publication_versions(self.project, "alpha", row, ("us",)), ("us",))

    def test_freebies_and_previously_published_versions_must_be_proved(self):
        row = self.record({"us": {"percent": 100, "exact": True}, "eu": {"percent": 100, "exact": True}})
        self.assertEqual(land.publication_versions(self.project, "alpha", row, ("us",)), ("us", "eu"))
        row = self.record({"us": {"percent": 100, "exact": True}, "eu": {"percent": 50, "exact": False}})
        path = self.project.version("eu").split
        path.write_text(path.read_text().replace("asm, alpha]", "c, alpha]"))
        with self.assertRaisesRegex(Held, "land.not_exact"):
            land.exact_attempt(self.project, "alpha", self.file, required_versions=("us",))

    def test_unknown_duplicate_empty_and_nonholding_scope_refuse(self):
        self.record({"us": {"percent": 100, "exact": True}, "eu": {"percent": 100, "exact": True}})
        for required in ((), ("typo",), ("us", "us")):
            with self.subTest(required=required), self.assertRaisesRegex(Held, "land.versions"):
                land.exact_attempt(self.project, "alpha", self.file, required_versions=required)
        path = self.project.version("eu").split
        path.write_text(path.read_text().replace("asm, alpha]", "data, alpha]"))
        with self.assertRaisesRegex(Held, "land.versions"):
            land.exact_attempt(self.project, "alpha", self.file, required_versions=("eu",))

    def test_required_percent_without_exact_relocations_and_rules_still_refuse(self):
        self.record({"us": {"percent": 100, "exact": False}, "eu": {"percent": 50, "exact": False}})
        with self.assertRaisesRegex(Held, "land.not_exact"):
            land.exact_attempt(self.project, "alpha", self.file, required_versions=("us",))
        self.file.write_text('int alpha(void) { __asm__("nop"); }\n')
        self.record({"us": {"percent": 100, "exact": True}, "eu": {"percent": 50, "exact": False}})
        with self.assertRaisesRegex(Held, "land.rules"):
            land.exact_attempt(self.project, "alpha", self.file, required_versions=("us",))

    def test_missing_required_compare_and_changed_source_refuse(self):
        self.record({"eu": {"percent": 100, "exact": True}})
        with self.assertRaisesRegex(Held, "comparison unavailable without retained native evidence"):
            land.exact_attempt(self.project, "alpha", self.file, required_versions=("us",))
        self.file.write_text("int alpha(void) { return 2; }\n")
        with self.assertRaisesRegex(Held, "land.not_compared"):
            land.exact_attempt(self.project, "alpha", self.file, required_versions=("us",))
