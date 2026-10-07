"""A real repeated BattleTanx layout travels with every published consumer."""

import re
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tests.preprocessor import expand, output
from tests.project_fixture import ProjectCase
from unbake import config, land, process
from unbake.config import Held
from unbake.decomp import checks
from unbake.fold import apply
from unbake.layout import map as ownership
from unbake.layout import split
from unbake.process import named
from unbake.work.tidy import tidy

FUNCTION = "func_8007EC38"
CONSUMER = "func_8007ED98"
HEADER = "span_1000/code_8007EBA0.h"
FIXTURE = Path(__file__).parent / "fixtures/battletanx_shared/src"


class SharedConsumerTests(ProjectCase):
    def setUp(self):
        super().setUp()
        for version in self.project.versions:
            for path in (self.project.version(version).split, self.project.version(version).symbols):
                text = path.read_text().replace("alpha", FUNCTION).replace("beta", CONSUMER)
                if path.suffix == ".yaml":
                    text = text.replace("name: main", "name: span_1000")
                    text = text.replace(f"asm, {FUNCTION}", f"c, {FUNCTION}")
                    text = text.replace(f"asm, {CONSUMER}", f"c, {CONSUMER}")
                path.write_text(text)
        (self.project.root / "layout.toml").write_bytes(
            ownership.encoded(
                ownership.Map(
                    2,
                    (
                        ownership.Group("code_8007EBA0", "span_1000", "default", (FUNCTION, CONSUMER)),
                        ownership.Group("gamma", "span_1000", "default", ("gamma",)),
                    ),
                )
            )
        )
        root = self.project.include[0]
        (root / "types.h").write_text(
            "#ifndef TYPES_H\n#define TYPES_H\ntypedef int s32; typedef unsigned int u32; "
            "typedef unsigned short u16;\n#endif\n"
        )
        (root / "n64sdk.h").write_text(
            "#ifndef SDK_H\n#define SDK_H\ntypedef union Gfx { struct { u32 w0, w1; } words; } Gfx;\n#endif\n"
        )
        (root / "gbi.h").write_text("/* SDK macro boundary */\n")
        self.header = root / HEADER
        self.header.parent.mkdir(parents=True)
        self.header.write_text("#ifndef GROUP_H\n#define GROUP_H\n#endif\n")
        for name in (FUNCTION, CONSUMER):
            (self.project.src / f"{name}.c").write_bytes((FIXTURE / f"{name}.c").read_bytes())
        self.project = config.load(self.project.root)
        self.file = self.project.work / FUNCTION / f"{FUNCTION}.c"
        self.file.parent.mkdir(parents=True)
        self.file.write_bytes((FIXTURE / f"{FUNCTION}.c").read_bytes())
        self.consumer = self.project.src / f"{CONSUMER}.c"
        self.original = self.consumer.read_text()
        self.stack = self.enterContext(ExitStack())
        self.stack.enter_context(patch.object(process.subprocess, "run", side_effect=output))
        self.stack.enter_context(
            patch(
                "tests.preprocessor.expand", side_effect=lambda *a, **kw: expand(*a, **{**kw, "preserve_columns": True})
            )
        )
        self.stack.enter_context(
            patch("unbake.fold.apply.gbi.prepare", side_effect=lambda p, t, m: SimpleNamespace(source=t, headers=()))
        )
        self.stack.enter_context(
            patch("unbake.typemap.declaration_evidence.inject", side_effect=lambda p, h, t, f, v: (t, 0))
        )
        self.stack.enter_context(
            patch("unbake.fold.declarations.gbi_recover.proven", side_effect=lambda p, h, f, t, *a, **k: t)
        )
        self.stack.enter_context(patch("unbake.fold.declarations.entries.owners", return_value=[]))
        self.stack.enter_context(patch("unbake.layout.structs_fold._prove_includers", side_effect=self.compile_overlay))
        # Keep the real snapshot intact while isolating aggregate promotion
        # from its legacy SDK macros and empty decompiler prelude.
        check_source = checks.run
        self.stack.enter_context(
            patch.object(
                checks,
                "run",
                side_effect=lambda source: [f for f in check_source(source) if f.rule == "invented-struct"],
            )
        )
        self.compiled = []
        self.stack.enter_context(
            patch("unbake.pool.run", side_effect=lambda host, fn, jobs, **kw: [fn(job) for job in jobs])
        )
        self.stack.enter_context(patch("unbake.runner.build_unit", side_effect=self.build_unit))
        self.stack.enter_context(
            patch.object(
                land,
                "_prove_versions",
                return_value={
                    self.header,
                    *(self.project.include[0] / name for name in ("types.h", "gbi.h", "n64sdk.h")),
                },
            )
        )
        self.stack.enter_context(patch.object(land, "exact_attempt", return_value=SimpleNamespace(compiler="ido-7.1")))
        self.stack.enter_context(patch.object(land.buildfiles, "write", return_value=[]))
        self.stack.enter_context(patch.object(land.steps, "record"))
        self.stack.enter_context(patch("unbake.report.progress.write", return_value=[]))
        self.fail_commit = False
        self.mismatch = None
        self.git = []
        self.stack.enter_context(patch.object(land, "_git", side_effect=self.git_command))

    def compile_overlay(self, project, edits, host, republished):
        promoted = set()
        for edit in edits:
            if edit.path.suffix == ".h":
                promoted.update(re.findall(r"\b(?:struct|union)\s+(\w+)\s*\{", edit.after))
        replacements = {edit.path: edit.after for edit in edits}
        for source in project.src.glob("*.c"):
            if source == republished:
                continue
            text = replacements.get(source, source.read_text())
            local = set(re.findall(r"\b(?:struct|union)\s+(\w+)\s*\{", text))
            if local & promoted:
                raise Held(
                    named(
                        "fixture.refusal",
                        f"redefinition in {source.name}: {sorted(local & promoted)}",
                        owner="fixture",
                        stage="structs",
                    )
                )

    def build_unit(self, project, host, unit, version, source=None):
        text = source.read_text()
        self.assertNotRegex(text, r"\b(?:struct|union)\s+func_8007EC38_S1\s*\{")
        self.compiled.append((unit, version, text))
        if (unit, version) == self.mismatch:
            return b"changed ROM bytes"
        row = next(row for row in split.functions(project, version) if Path(row.path).name == unit)
        return split.words(project, row)

    def git_command(self, project, *args, env=None):
        self.git.append(args)
        if args[:2] == ("rev-parse", "--git-path"):
            return str(project.root / ".git/index")
        if args[:2] == ("ls-tree", "-r"):
            return "\0".join(str(path.relative_to(project.root)) for path in project.include[0].rglob("*.h")) + "\0"
        if args == ("rev-parse", "HEAD"):
            return "c0ffee\n"
        if "commit" in args and self.fail_commit:
            raise Held(named("fixture.refusal", "hook refused", owner="fixture", stage="land"))
        return ""

    def test_real_published_consumer_is_rewritten_in_the_promotion_commit(self):
        self.assertEqual(land.land(self.project, self.host, self.file), "c0ffee")
        self.assertNotIn("struct Func8007EC38Entry {", self.consumer.read_text())
        self.assertIn("struct Func8007EC38Entry {", self.header.read_text())
        self.assertEqual({(u, v) for u, v, _ in self.compiled}, {(CONSUMER, "us"), (CONSUMER, "eu")})
        added = next(args for args in self.git if args[0] == "add")
        self.assertIn(f"src/{CONSUMER}.c", added)
        self.assertIn(f"include/{HEADER}", added)
        self.assertEqual(len([args for args in self.git if "commit" in args]), 1)

    def test_tidy_is_private_and_land_reconstructs_the_consumer_transaction(self):
        tidy(self.project, self.host, self.file)
        self.assertEqual(self.consumer.read_text(), self.original)
        self.assertNotIn("struct Func8007EC38Entry {", self.file.read_text())
        self.assertEqual(land.land(self.project, self.host, self.file), "c0ffee")
        self.assertNotIn("struct Func8007EC38Entry {", self.consumer.read_text())
        self.assertEqual(len(self.compiled), 2)
        self.assertIn("struct Func8007EC38Entry {", self.header.read_text())

    def test_different_field_names_are_rewritten_by_expression_type(self):
        self.consumer.write_text(self.original.replace("threshold", "minimum").replace("unkD8", "display"))
        folded = apply.fold(self.project, self.host, FUNCTION, self.file.read_text())
        rewritten = next(edit.after for edit in folded.source_edits if edit.path == self.consumer)
        self.assertIn("->unkD8", rewritten)
        self.assertIn(".threshold", rewritten)
        self.assertNotIn("minimum", rewritten)
        self.assertNotIn("display", rewritten)

    def test_scalar_typedef_spelling_resolves_to_the_same_layout(self):
        self.consumer.write_text(
            self.original.replace("s32 threshold;", "Threshold threshold;").replace(
                "struct Func8007EC38Entry {", "typedef s32 Threshold;\nstruct Func8007EC38Entry {"
            )
        )
        folded = apply.fold(self.project, self.host, FUNCTION, self.file.read_text())
        rewritten = next(edit.after for edit in folded.source_edits if edit.path == self.consumer)
        self.assertNotIn("Threshold threshold;", rewritten)
        self.assertIn(".threshold", rewritten)

    def test_different_layout_gets_a_distinct_promotion_tag(self):
        self.consumer.write_text(self.original.replace("s32 threshold;", "u32 threshold;"))
        folded = apply.fold(self.project, self.host, FUNCTION, self.file.read_text())
        self.assertIn(f"Func8007EC38Entry_{FUNCTION}", folded.source)
        self.assertIn(f"struct Func8007EC38Entry_{FUNCTION}", folded.headers[HEADER])
        rewritten = next(edit.after for edit in folded.source_edits if edit.path == self.consumer)
        self.assertIn("u32 threshold;", rewritten)
        self.assertNotIn(f"Func8007EC38Entry_{FUNCTION}", rewritten)

    def test_nonpublished_branch_is_not_rewritten_or_proved(self):
        path = self.project.version("eu").split
        path.write_text(path.read_text().replace(f"c, {CONSUMER}", f"asm, {CONSUMER}"))
        land.land(self.project, self.host, self.file)
        self.assertEqual([(u, v) for u, v, _ in self.compiled], [(CONSUMER, "us")])

    def test_consumer_byte_mismatch_refuses_without_writing(self):
        before = self.header.read_bytes()
        self.mismatch = (CONSUMER, "eu")
        with self.assertRaisesRegex(Held, "changed default ROM code"):
            land.land(self.project, self.host, self.file)
        self.assertEqual(self.header.read_bytes(), before)
        self.assertEqual(self.consumer.read_text(), self.original)
        self.assertFalse(any("commit" in args for args in self.git))

    def test_commit_failure_restores_source_header_and_consumer(self):
        before = {path: path.read_bytes() for path in (self.header, self.consumer, self.project.src / f"{FUNCTION}.c")}
        self.fail_commit = True
        with self.assertRaisesRegex(Held, "hook refused"):
            land.land(self.project, self.host, self.file)
        self.assertEqual({path: path.read_bytes() for path in before}, before)

    def test_staged_layout_conflict_names_both_units(self):
        tidy(self.project, self.host, self.file)
        self.consumer.write_text(self.original.replace("s32 threshold;", "u32 threshold;"))
        with self.assertRaisesRegex(Held, rf"{FUNCTION}.c and {CONSUMER}.c.*conflicting published layout"):
            apply.fold(self.project, self.host, FUNCTION, self.file.read_text())

    def test_consumer_changed_during_proof_is_not_overwritten(self):
        changed = self.original + "/* concurrent edit */\n"
        build = self.build_unit

        def concurrent(project, host, unit, version, source=None):
            data = build(project, host, unit, version, source)
            self.consumer.write_text(changed)
            return data

        self.stack.enter_context(patch("unbake.runner.build_unit", side_effect=concurrent))
        before = self.header.read_bytes()
        with self.assertRaisesRegex(Held, "source changed since proof"):
            land.land(self.project, self.host, self.file)
        self.assertEqual(self.consumer.read_text(), changed)
        self.assertEqual(self.header.read_bytes(), before)
        self.assertFalse(any("commit" in args for args in self.git))

    def test_existing_equal_header_does_not_reintroduce_a_reserved_tag(self):
        (self.project.include[0] / "entry.h").write_text(
            '#include "types.h"\nstruct Func8007EC38Entry { s32 threshold; char pad4[0x14]; };\n'
        )
        self.consumer.write_text(self.original.replace("s32 threshold;", "u32 threshold;"))
        folded = apply.fold(self.project, self.host, FUNCTION, self.file.read_text())
        self.assertIn(f"Func8007EC38Entry_{FUNCTION}", folded.source)
        self.assertIn(f"struct Func8007EC38Entry_{FUNCTION}", folded.headers[HEADER])
