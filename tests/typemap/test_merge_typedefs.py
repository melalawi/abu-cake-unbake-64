"""Published declarations of one global agree when they name one type through a typedef; real differences conflict."""

import unittest
from types import SimpleNamespace

from unbake.typemap import solver

ALIASES = {"s32": "int", "u32": "unsigned int"}


def seed(type_: str, source: str, kind: str = "published") -> dict:
    provenance = {"kind": kind, "source": source}
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
                self.assertEqual(bool(graph.facts), conflict)

    def test_a_published_contract_replaces_an_equal_declared_one(self) -> None:
        """The RW loss: the header's declared int came first; the consumer's published int must win, or the
        flow conflict drops the global from the header its consumer needs."""
        for label, first, second, kind in [
            ("declared then published", ("int", "declared"), ("int", "published"), "published"),
            ("declared then published s32", ("int", "declared"), ("s32", "published"), "published"),
            ("published then declared", ("int", "published"), ("int", "declared"), "published"),
            (
                "near miss: different types keep the conflict rules",
                ("int", "declared"),
                ("float", "published"),
                "published",
            ),
        ]:
            with self.subTest(label):
                graph = SimpleNamespace(facts=[])
                seeds = [seed(first[0], "h.h", first[1]), seed(second[0], "c.c", second[1])]
                record = solver._merge_records(seeds, "globals", graph)["D_800CD910_de"]
                self.assertEqual(record["provenance"]["kind"], kind)
