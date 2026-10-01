"""Balanced macro lowering and variant-specific inferred layout recovery."""

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

    def test_conflicting_inferred_layouts_have_distinct_repeatable_names(self) -> None:
        with tempfile.TemporaryDirectory(dir=os.environ["TMPDIR"]) as directory:
            project, _, _ = fixture(Path(directory))
            header = project.include[0] / "structs.h"
            original = "struct Layout_alpha_p { char padding[4]; float field_4; };\n"
            header.write_text(original)
            context = "typedef int s32;\n" + original
            output, shared = share(project, "alpha", "s32 alpha(void *p) { return M2C_FIELD(p, s32 *, 4); }", context)
            self.assertEqual(shared, header)
            self.assertIn(original.strip(), header.read_text())
            self.assertIn("Layout_alpha_p_", output)
            repeated, _ = share(
                project,
                "alpha",
                "s32 alpha(void *p) { return M2C_FIELD(p, s32 *, 4); }",
                "typedef int s32;\n" + header.read_text(),
            )
            self.assertEqual(output, repeated)
            c_parser.CParser().parse("typedef int s32;\n" + header.read_text() + output)
