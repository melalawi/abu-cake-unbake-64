"""unit_layouts.records resumed from a shared header prefix equals the records of the whole unit."""

import unittest
from typing import Any, ClassVar

from unbake import prefixes
from unbake.cdecl import LayoutParser
from unbake.config import Held
from unbake.typemap import unit_layouts

HEADER = "struct Fwd;\ntypedef struct Fwd Fwd;\n" + "".join(
    f"typedef struct S{i} {{\n    int a;\n    char b[{i + 1}];\n}} T{i};\n" for i in range(300)
)


def whole(source: str, aliases: dict[str, str]) -> tuple[dict[str, Any], list[str]]:
    """The records of an unresumed layout pass, as the facts produced them before resumption."""
    rows: dict[str, Any] = {}
    try:
        for layout in LayoutParser(source).parse():
            if layout.fields:
                rows[layout.name] = unit_layouts._row(layout, source, aliases)
    except Held as error:
        return rows, ["types.layout: " + error.reason]
    return rows, []


class ResumedLayoutTests(unittest.TestCase):
    RESTS: ClassVar[dict[str, tuple[str, dict[str, str]]]] = {
        "externs only": ("extern T1 *p;\n", {}),
        "a new alias of a prefix struct": ("typedef struct S2 Again;\n", {"Again": "struct S2"}),
        "a prefix forward struct completed": ("struct Fwd {\n    T3 t;\n};\n", {}),
        "a new struct using prefix structs by value": ("struct New {\n    T4 t;\n    Fwd *f;\n};\n", {}),
        "a typedef name a prefix record reads": ("extern int x;\n", {"T5": "struct S5"}),
        "a missing layout": ("struct Bad {\n    Missing m;\n};\n", {}),
        "a rebound prefix typedef": ("typedef struct S1 T0;\n", {"T0": "struct S1"}),
    }

    def test_every_rest_matches_the_whole_unit(self) -> None:
        base = {f"T{i}": f"struct S{i}" for i in range(300)}
        for order in (list(self.RESTS), list(reversed(self.RESTS))):
            prefixes.forget()
            for label in (*order, *order):
                rest, extra = self.RESTS[label]
                with self.subTest(label):
                    source = HEADER + rest
                    aliases = {**base, **extra}
                    self.assertEqual(unit_layouts.records(source, aliases), whole(source, aliases))


if __name__ == "__main__":
    unittest.main()
