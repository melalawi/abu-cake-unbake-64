"""A unit's facts never depend on which unit the same process extracted before it."""

import unittest
from pathlib import Path

from unbake import cache, prefixes
from unbake.typemap import declarations

HEADER = "typedef struct { int value; } Kind;\n"


def unit(body: str) -> str:
    return declarations._BOUNDARY + "\n" + HEADER + body


def structs(body: str) -> list[str]:
    seed = declarations.published(unit(body), {"kind": "proven"}, Path("src/x.c"), compact=True)
    return sorted(seed["structs"])


class UnitOrderTests(unittest.TestCase):
    def setUp(self) -> None:
        cache._memo.clear()
        prefixes.forget()

    def test_structs_follow_the_unit_not_the_previous_one(self) -> None:
        # (earlier unit, later unit, its layouts); the same anonymous typedef, different aggregates.
        cases = [
            ("struct A { int a; };\n", "struct B { int b; };\n", ["B", "Kind"]),
            ("struct A { int a; };\n", "struct A { int a; };\nstruct C { short c; };\n", ["A", "C", "Kind"]),
            ("struct A { int a; };\n", "int no_layouts;\n", ["Kind"]),
            # Near miss: a different typedef set never shared a result.
            ("typedef int Other;\nstruct A { int a; };\n", "struct B { int b; };\n", ["B", "Kind"]),
        ]
        for earlier, later, expected in cases:
            with self.subTest(later=later):
                self.setUp()
                fresh = structs(later)
                self.setUp()
                structs(earlier)
                self.assertEqual(structs(later), fresh)
                self.assertEqual(fresh, expected)
