"""Merging layout-template receipts: a repeated template takes the identity shortcut and gives the full-path result."""

import copy
from types import SimpleNamespace

from tests.kit import TempCase
from unbake.typemap import declarations, solver

ALIASES = {"s32": "int"}


def layout(field: str) -> dict:
    return {"S": {"state": "known", "fields": [{"name": "a", "type": field}], "declaration": "struct S { };"}}


def seed(template: dict, kind: str, source: str) -> dict:
    return {"aliases": ALIASES, "structs": declarations.ProvenStructs(template, {"kind": kind, "source": source})}


class TemplateReceiptTests(TempCase):
    def test_repeats_and_spellings_and_conflicts_merge_as_distinct_objects_do(self) -> None:
        int_layout, float_layout = layout("int"), layout("float")
        spec = [
            (int_layout, "declared", "a.c"),
            (float_layout, "declared", "b.c"),  # a conflict
            (int_layout, "published", "c.c"),  # a repeat of the first, not consecutive, at a higher rank
            (layout("s32"), "declared", "d.c"),  # a typedef-equal spelling
            (int_layout, "declared", "e.c"),  # a repeat at a lower rank: the published receipt stays
            (int_layout, "published", "f.c"),
        ]
        results = []
        for shared in (True, False):
            seeds = [
                seed(template if shared else copy.deepcopy(template), kind, source) for template, kind, source in spec
            ]
            graph = SimpleNamespace(facts=[])
            results.append((solver._merge_records(seeds, "structs", graph), graph.facts))
        self.assertEqual(results[0], results[1])
        merged = results[0][0]["S"]
        self.assertEqual(merged["provenance"]["source"], "f.c")
        self.assertTrue(merged["declaration_conflict"])
