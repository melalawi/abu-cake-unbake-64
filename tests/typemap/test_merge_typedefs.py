"""Published declarations of one global agree when they name one type through a typedef; real differences conflict."""

import unittest
from types import SimpleNamespace

from unbake.typemap import solver

ALIASES = {"s32": "int", "u32": "unsigned int"}


def seed(type_: str, source: str) -> dict:
    provenance = {"kind": "published", "source": source}
    record = {"type": type_, "declaration": f"extern {type_} D_800CD910_de;", "provenance": provenance}
    return {"aliases": ALIASES, "globals": {"D_800CD910_de": record}}


class MergeTests(unittest.TestCase):
    def test_typedef_spellings_of_one_type(self) -> None:
        for label, left, right, conflict in [
            ("s32 and int are one type", "s32", "int", False),
            ("the reverse order", "int", "s32", False),
            ("near miss: s32 and u32 differ in sign", "s32", "u32", True),
            ("negative: int and float", "int", "float", True),
        ]:
            with self.subTest(label):
                graph = SimpleNamespace(facts=[])
                merged = solver._merge_records([seed(left, "a.c"), seed(right, "b.c")], "globals", graph)
                record = merged["D_800CD910_de"]
                self.assertEqual(record["declaration_conflict"], conflict)
                self.assertEqual(record["type"], left)  # the first published spelling stays
                self.assertEqual(bool(graph.facts), conflict)
