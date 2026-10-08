"""Fold drops a local redeclaration whose canonical type equals the shared one and refuses real mismatches."""

from unittest import TestCase

from unbake.config import Held
from unbake.layout import redeclarations

TYPES = "typedef int s32; typedef unsigned char u8; typedef s32 word;\n"


class RedeclarationTypedefTests(TestCase):
    def strip(self, local, shared):
        return redeclarations.strip(TYPES + local, [TYPES + shared])

    def test_typedef_equals_builtin(self):
        self.assertNotIn("D_1", self.strip("extern s32 D_1;\n", "extern int D_1;\n"))

    def test_typedef_chain_and_unsigned_char(self):
        self.assertNotIn("D_2", self.strip("extern word D_2;\n", "extern int D_2;\n"))
        self.assertNotIn("D_3", self.strip("extern u8 D_3;\n", "extern unsigned char D_3;\n"))

    def test_pointer_to_typedef(self):
        self.assertNotIn("D_4", self.strip("extern u8 *D_4;\n", "extern unsigned char *D_4;\n"))

    def test_multi_declarator_split_keeps_unshared(self):
        out = self.strip("extern s32 D_5, D_6;\n", "extern int D_5;\n")
        self.assertNotIn("D_5", out)
        self.assertIn("extern s32 D_6;", out)

    def test_multi_declarator_split_keeps_pointer_specifier(self):
        out = self.strip("extern u8 D_7, *D_8;\n", "extern unsigned char D_7;\n")
        self.assertNotIn("D_7", out)
        self.assertIn("extern u8 *D_8;", out)

    def test_real_mismatch_refused_with_both_types(self):
        with self.assertRaises(Held) as raised:
            self.strip("extern s32 D_9, D_10;\n", "extern short D_9;\n")
        reason = raised.exception.reason
        self.assertIn("layout.redeclaration.D_9", reason)
        self.assertIn("extern s32 D_9, D_10;", reason)
        self.assertIn("extern short D_9;", reason)
        self.assertIn("local canonical type:", reason)
        self.assertIn("shared canonical type:", reason)
