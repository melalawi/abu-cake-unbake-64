"""Shared declaration preflight and final source regressions."""

import hashlib
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import asdict
from itertools import pairwise
from pathlib import Path
from unittest.mock import patch

from tests.match.support import MatchFixture
from unbake.decomp import needs
from unbake.layout.structs import layouts
from unbake.layout.structs_parser import Parser
from unbake.match import data_symbols, declarations
from unbake.match import queue as match
from unbake.match.data_symbols import prepare
from unbake.project import build as project_build
from unbake.project.config import Held, Policy, Project


class DeclarationTests(MatchFixture):
    def test_existing_c_row_with_rodata_can_be_resubmitted(self) -> None:
        source = self.src / "alpha.c"
        source.write_text("int alpha(void) {return 1;}\n")
        for version in self.versions:
            path = self.project.version(version).split
            text = path.read_text().replace("asm, text/alpha]", "c, alpha]")
            text = text.replace("c, alpha]\n", "c, alpha]\n      - [0x1008, .rodata, alpha]\n")
            path.write_text(text)
        edits = declarations.match_edits(self.project, "alpha", source.read_text(), self.versions)
        self.assertEqual([edit.path for edit in edits], [source])

    def test_imported_value_and_callback_types_are_included_in_folded_header(self) -> None:
        header = self.root / "include" / "types.h"
        header.write_text(
            "#ifndef TYPES_H\n#define TYPES_H\n"
            "typedef struct Vec3f {float x,y,z;} Vec3f;\n"
            "typedef int (*Callback)(void *);\n#endif\n"
            "typedef char imported_size_check[(sizeof(Vec3f) == 12) ? 1 : -1];\n"
        )
        text = '#include "types.h"\nstruct Holder {Vec3f position;Callback handler;};\n'
        text += "int alpha(struct Holder *p) {return p->handler(p);}\n"
        edits = declarations.folded_edits(self.project, self.policy, "alpha", text, self.versions)
        destination = self.root / "include" / "shared" / "alpha.h"
        generated = next(edit.after for edit in edits if edit.path == destination)
        self.assertIn('#include "types.h"', generated)
        for edit in edits:
            edit.path.parent.mkdir(parents=True, exist_ok=True)
            edit.path.write_text(edit.after)
        subprocess.run(
            ["cc", "-std=c89", "-fsyntax-only", "-I", str(self.root / "include"), str(self.src / "alpha.c")],
            capture_output=True,
            text=True,
            check=True,
        )

    def test_opaque_pointer_alias_does_not_escape_into_shared_header(self) -> None:
        text = "typedef struct Opaque_s Opaque;\nstruct Holder {Opaque *pointer;struct Opaque_s *other;};\n"
        text += "int alpha(struct Holder *p) {return p->pointer != 0;}\n"
        edits = declarations.folded_edits(self.project, self.policy, "alpha", text, self.versions)
        destination = self.root / "include" / "shared" / "alpha.h"
        generated = next(edit.after for edit in edits if edit.path == destination)
        self.assertIn("struct Opaque_s *pointer;", generated)
        for edit in edits:
            edit.path.parent.mkdir(parents=True, exist_ok=True)
            edit.path.write_text(edit.after)
        subprocess.run(
            ["cc", "-std=c89", "-fsyntax-only", "-I", str(self.root / "include"), str(self.src / "alpha.c")],
            capture_output=True,
            text=True,
            check=True,
        )

    def test_version_branches_fold_and_compile_before_queueing(self) -> None:
        text = (
            "extern int table;\ntypedef struct Record Record;\nstruct Record { int value; };\n"
            "typedef struct { int value; } Other;\n"
            "int alpha(Record *arg, Other *other) {\n"
            "#if defined(VERSION_US)\nif (arg->value) {\n"
            "#else\nif (other->value) {\n#endif\nreturn arg->value + table; } return 0; }\n"
        )
        source = self.draft("alpha", text)
        compiler = shutil.which("cc")
        assert compiler is not None
        compiled = []

        def compile_source(project: Project, policy: Policy, source: Path, version: str, out: Path) -> Path:
            completed = subprocess.run(
                [
                    compiler,
                    "-std=c89",
                    "-fsyntax-only",
                    f"-DVERSION_{version.upper()}",
                    f"-I{project.include[0]}",
                    str(source),
                ],
                capture_output=True,
                text=True,
            )
            if completed.returncode:
                raise Held("compile", completed.stderr)
            compiled.append(version)
            return out

        with (
            patch.object(data_symbols, "prepare", prepare),
            patch.object(data_symbols, "edits", return_value=[]),
            patch.object(project_build, "compile_object", side_effect=compile_source),
        ):
            match.submit(self.project, self.policy, source)
        self.assertEqual(compiled, list(self.versions))
        self.assertFalse((self.root / "include" / "shared" / "alpha.h").exists())
        self.assertTrue(any("alpha matched" in receipt for receipt in match.run(self.project, self.policy)))
        self.assertIn('"shared/alpha.h"', (self.src / "alpha.c").read_text())
        self.assertIn("struct Record {", (self.root / "include" / "shared" / "alpha.h").read_text())
        invalid = self.draft(
            "beta",
            text.replace("alpha(", "beta(").replace("return arg->value + table;", "return arg->missing + table;"),
        )
        with (
            patch.object(data_symbols, "prepare", prepare),
            patch.object(data_symbols, "edits", return_value=[]),
            patch.object(project_build, "compile_object", side_effect=compile_source),
            self.assertRaisesRegex(Held, "folded source compile failed.*VERSION us"),
        ):
            match.submit(self.project, self.policy, invalid)
        self.assertEqual(self.queued(), [])

    def test_folded_scalar_typedefs_use_project_home_or_refuse_by_name(self) -> None:
        header = self.root / "include" / "basetypes.h"
        header.write_text(
            "#ifndef BASETYPES_H\n#define BASETYPES_H\n"
            "typedef signed char s8; typedef unsigned char u8; typedef short s16; "
            "typedef int s32; typedef unsigned int u32; typedef long long s64; "
            "typedef float f32; typedef double f64; typedef int ScalarAlias;\n#endif\n"
        )
        (self.root / "include" / "structs.h").write_text('#include "basetypes.h"\nstruct Record { s32 value; };\n')
        cases = (
            ("typedef signed int s32; typedef unsigned char u8;", ""),
            ("typedef signed short s16; typedef unsigned char u8, LocalByte;", ""),
            ('#include "basetypes.h"\ntypedef signed int s32;', ""),
            ("typedef int Integer; typedef Integer s32;", ""),
            ("typedef int ScalarAlias;", ""),
            ("typedef signed char u8;", "u8"),
            ("typedef unsigned int s32;", "s32"),
            ("typedef long s32;", "s32"),
            ("typedef int *s32;", "s32"),
            ("typedef int s32[1];", "s32"),
            ("typedef const int s32;", "s32"),
            ("typedef struct Other { int value; } u8;", "u8"),
            ("typedef unsigned int s32; typedef int s32;", "s32"),
        )
        for aliases, conflict in cases:
            with self.subTest(aliases=aliases):
                body = "int alpha(struct Record *arg) { return arg->value; }"
                field_type = "ScalarAlias" if "ScalarAlias" in aliases else "s32"
                text = f"{aliases}\nstruct Record {{ {field_type} value; }};\n/* retained */\n{body}\n"
                if conflict:
                    with self.assertRaisesRegex(Held, rf"{conflict}: conflicting draft scalar typedef"):
                        declarations.final_source(self.project, text, [Parser(text)], [])
                    continue
                final = declarations.final_source(self.project, text, [Parser(text)], [])
                self.assertEqual(final.count('#include "basetypes.h"'), 1)
                self.assertIn(body, final)
                self.assertIn("/* retained */", final)
                self.assertNotIn("typedef signed int s32;", final)
                if "LocalByte" in aliases:
                    self.assertIn("typedef unsigned char LocalByte;", final)
                compiler = shutil.which("cc")
                assert compiler is not None
                result = subprocess.run(
                    [
                        compiler,
                        "-std=c89",
                        "-Wno-long-long",
                        "-pedantic-errors",
                        "-fsyntax-only",
                        "-x",
                        "c",
                        f"-I{self.root / 'include'}",
                        "-",
                    ],
                    input=final,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_forward_typedef_spans_preserve_externs_and_function_body(self) -> None:
        for kind in ("struct", "union"):
            with self.subTest(kind=kind):
                forwards = [f"typedef {kind} {name} {name};" for name in ("First", "Second", "Third")]
                definitions = [f"{kind} {name} {{ int value; }};" for name in ("First", "Second", "Third")]
                extern = "extern Second *global;"
                body = "int alpha(First *arg) { return arg->value + global->value; }"
                text = "\n".join([*forwards, definitions[0], extern, *definitions[1:], body]) + "\n"
                parser = Parser(text)
                records = parser.parse()
                self.assertEqual(
                    [text[item.start : item.end] for item in parser.declarations],
                    [
                        *forwards,
                        *definitions,
                    ],
                )
                self.assertEqual(
                    [text[item.start : item.end] for item in records], [definition[:-1] for definition in definitions]
                )
                self.assertTrue(all(first.end <= second.start for first, second in pairwise(records)))
                (self.root / "include" / "structs.h").write_text("\n".join([*forwards, *definitions]))
                final = declarations.final_source(self.project, text, [Parser(text)], [])
                self.assertIn(extern, final)
                self.assertIn(body, final)
                for declaration in (*forwards, *definitions):
                    self.assertNotIn(declaration, final)

    def test_shared_header_edits_acquire_and_check_unselected_versions(self) -> None:
        for outcome in ("success", "missing", "compare", "scoped"):
            with self.subTest(outcome=outcome):
                if outcome != "success":
                    self.doCleanups()
                    self.setUp()
                # The other cartridge already compiles this source, but its body
                # is not part of the queued function's trial proof.
                cartridge = self.project.version("eu")
                cartridge.split.write_text(cartridge.split.read_text().replace("asm, text/alpha", "c, alpha"))
                text = "struct Record { short x, y, z; };\nint alpha(void) { return 0; }\n"
                pending = self.pending(text)[:1] if outcome != "scoped" else []
                if outcome == "scoped":
                    text = "int alpha(void) { return 0; }\n"
                source = self.draft("alpha", text, pending=pending)
                match.submit(self.project, self.policy, source)
                if outcome == "missing":
                    self.project.build_link("eu").unlink()
                elif outcome == "compare":
                    self.build_failures.add(("alpha", "eu"))
                with patch.object(project_build, "build", wraps=self.build) as build:
                    receipts = match.run(self.project, self.policy)
                header = self.root / "include" / "shared" / "alpha.h"
                if outcome == "missing":
                    self.assertTrue(any("eu" in line and "HELD" in line for line in receipts), receipts)
                    build.assert_not_called()
                else:
                    self.assertEqual(build.call_args.args[2], ["us", "eu"])
                if outcome in ("success", "scoped"):
                    self.assertTrue(any("alpha matched" in line for line in receipts), receipts)
                    self.assertEqual(self.matched()[0]["versions"], ["us", "eu"])
                    self.assertEqual(header.exists(), outcome == "success")
                    self.assertEqual(self.current(self.project, "eu").name, "eu.1")
                else:
                    self.assertTrue(any("eu" in line and "HELD(match)" in line for line in receipts), receipts)
                    self.assertFalse(header.exists())
                    self.assertFalse((self.src / "alpha.c").exists())
                    self.assertEqual(len(self.queued()), 1)

    def pending(self, text: str) -> list[needs.Need]:
        record = layouts(text)[0]
        return [
            needs.LayoutNeed(
                version,
                record.name,
                [asdict(member) for member in record.fields],
                str(self.sources / "alpha.c"),
                dict(kind=record.kind, size=record.size, alignment=record.alignment, aliases=list(record.aliases)),
            )
            for version in self.versions
        ]

    def test_conflict_is_refused_before_staging_or_build(self) -> None:
        header = self.root / "include" / "structs.h"
        header.write_text("struct Record { int value; };\n")
        text = "struct Record { short value; };\nint alpha(void) { return 0; }\n"
        source = self.draft("alpha", text, pending=self.pending(text))
        self.prove(source)
        with self.assertRaisesRegex(Held, "Record.value"):
            match.submit(self.project, self.policy, source)
        with self.assertRaisesRegex(Held, "Record.value"):
            declarations.preflight(self.project, self.policy, self.pending(text))
        header.write_text("struct Record { short value; };\n")
        self.prove(source)
        match.submit(self.project, self.policy, source)
        header.write_text("struct Record { int value; };\n")
        self.assertTrue(any("submit.overlay_sha256" in line for line in match.run(self.project, self.policy)))
        self.assertEqual(self.calls, [])
        self.assertFalse(list((self.root / "build" / "match").glob("run-*")))

    def test_landing_creates_and_extends_one_shared_home(self) -> None:
        for function, name in (("alpha", "Record"), ("beta", "Other")):
            text = (
                f"typedef struct {name} {{ char pad[4]; int value; }} {name};\nint {function}(void) {{ return 0; }}\n"
            )
            source = self.draft(function, text, pending=self.pending(text))
            match.submit(self.project, self.policy, source)
            self.assertTrue(any(f"{function} matched" in line for line in match.run(self.project, self.policy)))
            landed = (self.src / source.name).read_text()
            self.assertIn(f'#include "shared/{function}.h"', landed)
            self.assertNotIn("typedef struct", landed)
        header = "\n".join(path.read_text() for path in (self.root / "include" / "shared").glob("*.h"))
        self.assertIn("struct Record", header)
        self.assertIn("struct Other", header)
        self.assertFalse((self.root / "include" / "alpha.h").exists())

    def test_header_promotion_keeps_queued_source_hash_through_retry(self) -> None:
        header = self.root / "include" / "structs.h"
        header.write_text("struct Record { char pad[4], tail[4]; };\n")
        text = "struct Record { int value; char tail[4]; };\nint alpha(void) { return 0; }\n"
        source = self.src / "alpha.c"
        source.write_text(text)
        self.prove(source, pending=self.pending(text))
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        match.submit(self.project, self.policy, source)

        def inspect(tree: Path, generation_for: Callable[[str], Path]) -> None:
            self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), digest)
            self.assertEqual(self.queued()[0]["source_sha256"], digest)
            self.assertIn("int value;", (tree / "include" / "structs.h").read_text())
            self.assertNotIn("struct Record", (tree / "src" / "alpha.c").read_text())

        self.on_build = inspect
        self.build_failures.add(("alpha", "us"))
        self.assertTrue(any("HELD(match)" in line for line in match.run(self.project, self.policy)))
        self.assertEqual(source.read_text(), text)
        self.build_failures.clear()
        receipts = match.run(self.project, self.policy)
        self.assertTrue(any("alpha matched" in line for line in receipts), receipts)
        self.assertEqual(self.matched()[0]["sha256"], digest)
        self.assertEqual(self.queued(), [])

    def test_landing_removes_draft_marker_and_preserves_other_comments(self) -> None:
        text = (
            "/* NON_MATCHING: draft of alpha; verify behavior and bytes before match. */\n"
            "/* purpose */\nint alpha(void) { return 0; }\n"
        )
        source = self.draft("alpha", text)
        match.submit(self.project, self.policy, source)
        self.assertTrue(any("alpha matched" in line for line in match.run(self.project, self.policy)))
        self.assertEqual((self.src / "alpha.c").read_text(), "/* purpose */\nint alpha(void) { return 0; }\n")
        self.assertEqual(source.read_text(), text)
