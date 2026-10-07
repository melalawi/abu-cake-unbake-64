"""Balanced macro lowering preserves unknown accesses without inventing types."""

import os
import tempfile
import unittest
from pathlib import Path

from pycparser import c_parser  # type: ignore[import-untyped]

from tests.decomp.support import fixture, solved
from unbake.cdecl import records
from unbake.config import Held
from unbake.decomp.draft_context import required_headers
from unbake.decomp.draft_macros import lower
from unbake.decomp.field_access import share
from unbake.typemap.declarations import clean


class DraftMacroTests(unittest.TestCase):
    def test_real_callback_copy_and_published_call_preserve_signature(self) -> None:
        payloads = Path(__file__).parent / "fixtures" / "callback_fields"
        context = (payloads / "contract.h").read_text()
        source = (payloads / "func_8011DFE0_us.c").read_text()
        with tempfile.TemporaryDirectory(dir=os.environ["TMPDIR"]) as directory:
            project, _, _ = fixture(Path(directory).resolve(), case=self)
            output, shared = share(project, "func_8011DFE0_us", source, context)
            self.assertIsNotNone(shared)
            header = shared.read_text()
            self.assertEqual(header.count("s32 (*value)(void *);"), 2)
            layouts = records(context + header)
            measured = [row for row in layouts if row.name.startswith("Measured_")]
            self.assertEqual(len(measured), 2)
            self.assertEqual(sorted(row.fields[-1].offset for row in measured), [0x10, 0x24])
            self.assertEqual([row.fields[-1].size for row in measured], [4, 4])
            self.assertEqual(output.count("->value"), 3)
            self.assertNotIn("M2C_FIELD", output)
            self.assertIn("func_8011E564(var_s3,", output)
            c_parser.CParser().parse(clean(context + header + output))
            from unbake.decomp.checks import run

            self.assertFalse(run(output))

    def test_declared_callback_field_uses_existing_member_without_writes(self) -> None:
        context = "typedef int s32; struct Existing { char pad[36]; s32 (*callback)(void *); };"
        with tempfile.TemporaryDirectory(dir=os.environ["TMPDIR"]) as directory:
            project, _, _ = fixture(Path(directory).resolve(), case=self)
            source = "void alpha(struct Existing *p) { M2C_FIELD(p, s32 (**)(void *), 0x24)(p); }"
            output, shared = share(project, "alpha", source, context)
            self.assertIsNone(shared)
            self.assertIn("(p)->callback(p)", output)
            c_parser.CParser().parse(context + output)

    def test_callback_signatures_remain_distinct_and_invalid_fields_hold(self) -> None:
        context = "typedef int s32;"
        with tempfile.TemporaryDirectory(dir=os.environ["TMPDIR"]) as directory:
            project, _, _ = fixture(Path(directory).resolve(), case=self)
            source = (
                "void alpha(void *p) { M2C_FIELD(p, s32 (**)(void *), 0x24)(p);"
                " M2C_FIELD(p, void (**)(s32), 0x24)(1); }"
            )
            output, shared = share(project, "alpha", source, context)
            self.assertEqual(len(records(context + "\n" + shared.read_text())), 2)
            self.assertIn("s32 (*value)(void *);", shared.read_text())
            self.assertIn("void (*value)(s32);", shared.read_text())
            c_parser.CParser().parse(clean(context + "\n" + shared.read_text() + output))
            for pointer in ("s32", "s32 (void *)", "s32 (*)(void *)", "s32 (**)(void *) extra"):
                with self.subTest(pointer=pointer), self.assertRaises(Held):
                    share(project, "alpha", "void alpha(void *p) { M2C_FIELD(p, " + pointer + ", 0); }", context)
            with self.assertRaisesRegex(Held, "unaligned field"):
                share(project, "alpha", "void alpha(void *p) { M2C_FIELD(p, s32 (**)(void *), 0x25); }", context)

    def test_placeholders_preserve_signed_nested_lvalues_and_declared_unknowns(self) -> None:
        with tempfile.TemporaryDirectory(dir=os.environ["TMPDIR"]) as directory:
            project, _, _ = fixture(Path(directory).resolve(), case=self)
            context = "typedef int s32; typedef int M2C_UNK;"
            output = (
                "s32 bits; M2C_UNK alpha(char *p) { "
                "M2C_FIELD((p + ((bits + 1) * 4)), s32 *, -0x10) = 7; "
                "return M2C_FIELD(&bits, s32 *, -4) + (s32)M2C_BITWISE(float, bits); }"
            )
            output, shared = share(project, "alpha", output, context)
            output = lower(output, context)
            selected = required_headers({Path("types.h"): context, Path("globals.h"): "extern s32 bits;"}, output)
            self.assertIn(Path("globals.h"), selected)
            self.assertNotIn("M2C_FIELD", output)
            self.assertNotIn("M2C_BITWISE", output)
            self.assertIn("[-1].value", output)
            c_parser.CParser().parse(context + (clean(shared.read_text()) if shared else "") + output)
            with self.assertRaisesRegex(Held, r"unresolved M2C_UNKNOWN at line 2:.*M2C_UNKNOWN"):
                lower("int alpha(void) {\n return M2C_UNKNOWN(1); }", context)
            with self.assertRaisesRegex(Held, "requires addressable value"):
                lower("M2C_BITWISE(float, bits + 1)", context)

    def test_unrelated_local_base_gets_separate_measured_view(self) -> None:
        with tempfile.TemporaryDirectory(dir=os.environ["TMPDIR"]) as directory:
            project, _, _ = fixture(Path(directory).resolve(), case=self)
            header = project.include[0] / "structs.h"
            original = "struct Layout_alpha_p { char padding[4]; float field_4; };\n"
            header.write_text(original)
            context = "typedef int s32;\n" + original
            output, shared = share(project, "alpha", "s32 alpha(void *p) { return M2C_FIELD(p, s32 *, 4); }", context)
            self.assertIsNotNone(shared)
            self.assertEqual(original, header.read_text())
            self.assertNotIn("Layout_alpha", output)
            self.assertIn("->value", output)
            self.assertIsNotNone(shared)
            from unbake.decomp.checks import run

            self.assertFalse([finding for finding in run(output) if finding.rule == "raw-offset"])
            repeated, _ = share(
                project,
                "alpha",
                "s32 alpha(void *p) { return M2C_FIELD(p, s32 *, 4); }",
                "typedef int s32;\n" + header.read_text(),
            )
            self.assertEqual(output, repeated)
            c_parser.CParser().parse("typedef int s32;\n" + header.read_text() + clean(shared.read_text()) + output)

    def test_declared_base_uses_an_existing_field_without_header_writes(self) -> None:
        with tempfile.TemporaryDirectory(dir=os.environ["TMPDIR"]) as directory:
            project, _, _ = fixture(Path(directory).resolve(), case=self)
            context = "typedef int s32; struct Existing { s32 value; };"
            output, shared = share(
                project, "alpha", "s32 alpha(struct Existing *p) { return M2C_FIELD(p, s32 *, 0); }", context
            )
            self.assertIn("(p)->value", output)
            self.assertIsNone(shared)
            self.assertEqual({path.name for path in project.include[0].glob("*.h")}, {"types.h"})

    def test_solved_common_source_uses_shared_fields_without_changing_declared_type(self) -> None:
        from unbake.decomp.checks import run
        from unbake.typemap.layouts import observed

        with tempfile.TemporaryDirectory(dir=os.environ["TMPDIR"]) as directory:
            project, _, _ = fixture(Path(directory).resolve(), case=self)
            accesses = [{"function": "alpha", "opcode": 0x23, "width": 4, "signedness": True, "partial": False}]
            shape = observed("Shape_test", "global:source", {4: accesses}, {}, ["alpha", "beta"])
            context = "typedef int s32; extern int source;\n" + shape["declaration"]
            with solved({"structs": {"Shape_test": shape}}) as types_path:
                output, shared = share(
                    project,
                    "alpha",
                    "s32 alpha(void) { s32 p; p = source; return M2C_FIELD(p, s32 *, 4); }",
                    context,
                    types_path=types_path,
                )
            self.assertIsNone(shared)
            self.assertIn("((struct Shape_test *)(p))->field_4", output)
            self.assertNotIn("M2C_FIELD", output)
            c_parser.CParser().parse(context + output)
            self.assertFalse([finding for finding in run(output) if finding.rule == "raw-offset"])
            with solved({"structs": {"Shape_test": shape}}) as types_path:
                changed, _ = share(
                    project,
                    "alpha",
                    "s32 alpha(void) { s32 p; p = source; p = 1; return M2C_FIELD(p, s32 *, 4); }",
                    context,
                    types_path=types_path,
                )
            self.assertNotIn("->field_4", changed)
            with solved({"structs": {"Shape_test": shape}}) as types_path:
                unrelated, _ = share(
                    project,
                    "gamma",
                    "s32 gamma(void) { return M2C_FIELD(source, s32 *, 4); }",
                    context,
                    types_path=types_path,
                )
            self.assertNotIn("->field_4", unrelated)

    def test_nested_solved_source_and_opaque_storage_preserve_typed_lvalues(self) -> None:
        from unbake.typemap.layouts import observed

        with tempfile.TemporaryDirectory(dir=os.environ["TMPDIR"]) as directory:
            project, _, _ = fixture(Path(directory).resolve(), case=self)
            access = {"function": "alpha", "opcode": 0x2B, "width": 4, "signedness": None, "partial": False}
            parent = observed("Shape_parent", "global:source", {4: [access]}, {}, ["alpha", "beta"])
            child = observed("Shape_child", "field:global:source:4", {8: [access]}, {}, ["alpha", "beta"])
            parent["base_nodes"] = ["global:source", "param:alpha:r4"]
            context = "typedef int s32; extern int source;\n" + parent["declaration"] + child["declaration"]
            with solved({"structs": {"Shape_parent": parent, "Shape_child": child}}) as types_path:
                output, _ = share(
                    project,
                    "alpha",
                    "void alpha(s32 p) { M2C_FIELD(M2C_FIELD(p, s32 *, 4), s32 *, 8) = 7; }",
                    context,
                    types_path=types_path,
                )
            self.assertIn("->unknown_4", output)
            self.assertIn("->unknown_8", output)
            self.assertNotIn("M2C_FIELD", output)
            c_parser.CParser().parse(context + output)
