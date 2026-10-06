"""A clean republish can retire its obsolete own volatile prototype without changing ABI."""

import unittest
from pathlib import Path
from unittest.mock import patch

from unbake.fold import self_prototype


class SelfPrototypeTests(unittest.TestCase):
    def test_only_equivalent_own_volatile_signature_is_retired(self) -> None:
        path = Path("include/group.h")
        header = "extern void alpha(volatile char *old);\nextern void beta(volatile char *p);\n"
        for source, expected in (
            ("void alpha(char *p) { }", True),
            ("int alpha(char *p) { return 1; }", False),
            ("void alpha(volatile char *p) { }", False),
        ):
            with (
                self.subTest(source=source),
                patch.object(self_prototype.cdecl, "parse", wraps=self_prototype.cdecl.parse),
            ):
                edits = self_prototype.unqualify({path: header}, source, "alpha", ("us", "eu"))
            self.assertEqual(bool(edits), expected)
            if edits:
                self.assertEqual(edits[0].before, header)
                self.assertEqual(edits[0].versions, ("us", "eu"))
                self.assertIn("alpha( char *old)", edits[0].after)
                self.assertIn("beta(volatile char *p)", edits[0].after)
