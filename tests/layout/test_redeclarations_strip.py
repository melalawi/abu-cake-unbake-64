"""A source's local declaration of a header-declared symbol is removed; a differing one is refused or recorded."""

import re
import unittest
from pathlib import Path

from unbake.config import Held
from unbake.layout.apply import rewrite
from unbake.layout.map import Group, Map
from unbake.layout.redeclarations import strip, uncovered

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


CONDITIONAL = "extern void\n#if defined(VERSION_US)\nf_us\n#else\nf_auto\n#endif\n(void);\nvoid h(void) {}\n"


class PartialConditionalTests(unittest.TestCase):
    """A per-version declaration the imports cover in part names its other homes instead of being refused."""

    def test_uncovered(self) -> None:
        for label, headers, expected in (
            ("no header names it", ["extern void g(void);\n"], set()),
            ("one branch is imported", ["extern void f_us(void);\n"], {"f_auto"}),
            ("both branches are imported", ["extern void f_us(void);\n", "extern void f_auto(void);\n"], set()),
            ("a plain declaration is never partial", ["extern void f_us(void);\n"], set()),
        ):
            with self.subTest(label):
                text = "extern void f_us(void);\n" if label.startswith("a plain") else CONDITIONAL
                self.assertEqual(uncovered(text, headers), expected)

    def test_strip_refuses_part_and_removes_whole(self) -> None:
        with self.assertRaisesRegex(Held, "partially imported conditional declaration"):
            strip(CONDITIONAL, ["extern void f_us(void);\n"])
        text = strip(CONDITIONAL, ["extern void f_us(void);\n", "extern void f_auto(void);\n"])
        self.assertNotIn("f_auto", text)

    def test_rewrite_imports_wanted_local_names(self) -> None:
        group = Group("g", "s", "default", ("h",))
        lookup = {"symbols": {"f_us": "s/a.h", "f_auto": "common/b.h"}, "headers": {}, "type_headers": {}}
        for wanted, expected in ((frozenset(), {"s/g.h"}), (frozenset({"f_auto"}), {"s/g.h", "common/b.h"})):
            with self.subTest(wanted=sorted(wanted)):
                text = rewrite(Path("h.c"), CONDITIONAL, "h", Map(32, (group,)), lookup, previous=set(), wanted=wanted)
                self.assertEqual(set(re.findall(r'#include "([^"]+)"', text)), expected)
