"""A source's local declaration of a header-declared symbol is removed; a differing one is refused or recorded."""

import unittest

from unbake.config import Held
from unbake.layout.redeclarations import strip

HEADER = "extern int f(void *p);\n"


class StripTests(unittest.TestCase):
    def test_local_duplicates(self) -> None:
        for name, local, removed, recorded in (
            ("same type", "int f(void *q);\n", True, {}),
            ("differing type is recorded", "short f();\n", True, {"f": ("short f();", "extern int f(void *p);")}),
            ("unrelated name stays", "int g(void);\n", False, {}),
            ("a macro alias stays", "#define f f_de\nint f(void *q);\n", False, {}),
        ):
            with self.subTest(name):
                found: dict[str, tuple[str, str]] = {}
                text = strip(local + "void h(void) {}\n", [HEADER], found)
                local = local.splitlines()[-1]
                self.assertEqual(local.strip() not in text, removed)
                self.assertEqual(found, recorded)

    def test_differing_type_is_refused_without_a_record(self) -> None:
        with self.assertRaisesRegex(Held, "layout.redeclaration.f"):
            strip("short f();\n", [HEADER])
