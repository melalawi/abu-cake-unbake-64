"""Balanced macro lowering preserves unknown accesses without inventing types."""

import os
import tempfile
import unittest
from pathlib import Path

from pycparser import c_parser  # type: ignore[import-untyped]

from tests.decomp.support import fixture
from unbake.decomp.draft_context import required_headers
from unbake.decomp.draft_macros import lower
from unbake.decomp.field_access import share
from unbake.project.config import Held


class DraftMacroTests(unittest.TestCase):
    def test_placeholders_preserve_signed_nested_lvalues_and_declared_unknowns(self) -> None:
        with tempfile.TemporaryDirectory(dir=os.environ["TMPDIR"]) as directory:
            project, _, _ = fixture(Path(directory))
            context = "typedef int s32; typedef int M2C_UNK;"
            output = (
                "s32 bits; M2C_UNK alpha(char *p) { "
                "M2C_FIELD((p + ((bits + 1) * 4)), s32 *, -0x10) = 7; "
                "return M2C_FIELD(&bits, s32 *, -4) + (s32)M2C_BITWISE(float, bits); }"
            )
            output, _ = share(project, "alpha", output, context)
            output = lower(output, context)
            selected = required_headers({Path("types.h"): context, Path("globals.h"): "extern s32 bits;"}, output)
            self.assertIn(Path("globals.h"), selected)
            self.assertNotIn("M2C_FIELD", output)
            self.assertNotIn("M2C_BITWISE", output)
            self.assertIn("+ (-0x10)", output)
            c_parser.CParser().parse(context + output)
            with self.assertRaisesRegex(Held, r"unresolved M2C_UNKNOWN at line 2:.*M2C_UNKNOWN"):
                lower("int alpha(void) {\n return M2C_UNKNOWN(1); }", context)
            with self.assertRaisesRegex(Held, "requires addressable value"):
                lower("M2C_BITWISE(float, bits + 1)", context)

    def test_unrelated_local_base_does_not_create_or_change_shared_layout(self) -> None:
        with tempfile.TemporaryDirectory(dir=os.environ["TMPDIR"]) as directory:
            project, _, _ = fixture(Path(directory))
            header = project.include[0] / "structs.h"
            original = "struct Layout_alpha_p { char padding[4]; float field_4; };\n"
            header.write_text(original)
            context = "typedef int s32;\n" + original
            output, shared = share(project, "alpha", "s32 alpha(void *p) { return M2C_FIELD(p, s32 *, 4); }", context)
            self.assertIsNone(shared)
            self.assertEqual(original, header.read_text())
            self.assertNotIn("Layout_alpha", output)
            self.assertIn("*(s32 *)((char *)(p) + (4))", output)
            repeated, _ = share(
                project,
                "alpha",
                "s32 alpha(void *p) { return M2C_FIELD(p, s32 *, 4); }",
                "typedef int s32;\n" + header.read_text(),
            )
            self.assertEqual(output, repeated)
            c_parser.CParser().parse("typedef int s32;\n" + header.read_text() + output)

    def test_declared_base_uses_an_existing_field_without_header_writes(self) -> None:
        with tempfile.TemporaryDirectory(dir=os.environ["TMPDIR"]) as directory:
            project, _, _ = fixture(Path(directory))
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
            project, _, _ = fixture(Path(directory))
            accesses = [{"function": "alpha", "opcode": 0x23, "width": 4, "signedness": True, "partial": False}]
            shape = observed("Shape_test", "global:source", {4: accesses}, {}, ["alpha", "beta"])
            context = "typedef int s32; extern int source;\n" + shape["declaration"]
            output, shared = share(
                project,
                "alpha",
                "s32 alpha(void) { s32 p; p = source; return M2C_FIELD(p, s32 *, 4); }",
                context,
                layouts={"Shape_test": shape},
            )
            self.assertIsNone(shared)
            self.assertIn("((struct Shape_test *)(p))->field_4", output)
            self.assertNotIn("M2C_FIELD", output)
            c_parser.CParser().parse(context + output)
            self.assertFalse([finding for finding in run(output) if finding.rule == "raw-offset"])
            changed, _ = share(
                project,
                "alpha",
                "s32 alpha(void) { s32 p; p = source; p = 1; return M2C_FIELD(p, s32 *, 4); }",
                context,
                layouts={"Shape_test": shape},
            )
            self.assertNotIn("->field_4", changed)
            unrelated, _ = share(
                project,
                "gamma",
                "s32 gamma(void) { return M2C_FIELD(source, s32 *, 4); }",
                context,
                layouts={"Shape_test": shape},
            )
            self.assertNotIn("->field_4", unrelated)

    def test_nested_solved_source_and_opaque_storage_preserve_typed_lvalues(self) -> None:
        from unbake.typemap.layouts import observed

        with tempfile.TemporaryDirectory(dir=os.environ["TMPDIR"]) as directory:
            project, _, _ = fixture(Path(directory))
            access = {"function": "alpha", "opcode": 0x2B, "width": 4, "signedness": None, "partial": False}
            parent = observed("Shape_parent", "global:source", {4: [access]}, {}, ["alpha", "beta"])
            child = observed("Shape_child", "field:global:source:4", {8: [access]}, {}, ["alpha", "beta"])
            parent["base_nodes"] = ["global:source", "param:alpha:r4"]
            context = "typedef int s32; extern int source;\n" + parent["declaration"] + child["declaration"]
            output, _ = share(
                project,
                "alpha",
                "void alpha(s32 p) { M2C_FIELD(M2C_FIELD(p, s32 *, 4), s32 *, 8) = 7; }",
                context,
                layouts={"Shape_parent": parent, "Shape_child": child},
            )
            self.assertIn("->unknown_4", output)
            self.assertIn("->unknown_8", output)
            self.assertNotIn("M2C_FIELD", output)
            c_parser.CParser().parse(context + output)
