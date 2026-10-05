"""The merge checks type meaning before it builds conflict views; results and named facts keep seed order."""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from unbake.typemap import declarations, solver

ALIASES = {"s32": "int", "u32": "unsigned int"}


def function(params: list[tuple[str, str]], kind: str, source: str, aliases: dict[str, str] = ALIASES) -> dict:
    record = {
        "return": "void",
        "params": [{"name": name, "type": type_} for name, type_ in params],
        "registers": ["r4"] * len(params),
        "provenance": {"kind": kind, "source": source},
    }
    return {"aliases": aliases, "functions": {"f": record}}


class MergeOrderTests(unittest.TestCase):
    def test_cases(self) -> None:
        for label, seeds, conflict, facts, first in [
            ("parameter names only differ", [(("a", "s32"),), (("b", "int"),)], False, [], "a"),
            (
                "near miss: sign differs, same rank",
                [(("a", "s32"),), (("a", "u32"),)],
                True,
                ["declaration_conflict"],
                "a",
            ),
            (
                "negative: repeated conflict names every repeat",
                [(("a", "int"),), (("a", "float"),), (("a", "float"),)],
                True,
                ["declaration_conflict", "declaration_conflict"],
                "a",
            ),
        ]:
            with self.subTest(label):
                graph = SimpleNamespace(facts=[])
                rows = [function(list(params), "published", f"{i}.c") for i, params in enumerate(seeds)]
                merged = solver._merge_records(rows, "functions", graph)["f"]
                self.assertEqual(merged["declaration_conflict"], conflict)
                self.assertEqual(merged["params"][0]["name"], first)
                self.assertEqual([fact["kind"] for fact in graph.facts], facts)

    def test_higher_rank_replaces_and_is_named(self) -> None:
        graph = SimpleNamespace(facts=[])
        seeds = [function([("a", "int")], "published", "a.c"), function([("a", "float")], "proven", "b.c")]
        merged = solver._merge_records(seeds, "functions", graph)["f"]
        self.assertEqual(merged["provenance"]["source"], "b.c")
        self.assertEqual([fact["kind"] for fact in graph.facts], ["published_contract_conflict"])
        self.assertEqual(graph.facts[0]["previous"]["params"], ["int"])

    def test_one_alias_environment_canonicalises_each_spelling_once(self) -> None:
        seeds = [function([("a", "s32")], "published", f"{i}.c") for i in range(50)]
        with patch.object(solver.declarations, "canonical", wraps=declarations.canonical) as canonical:
            solver._merge_records(seeds, "functions", SimpleNamespace(facts=[]))
        self.assertEqual(canonical.call_count, 2)  # "s32" (parameter) and "void" (return)

    def test_distinct_alias_maps_are_not_confused(self) -> None:
        # Near miss: equal ids must never reuse another map's spellings; each map is held by the merge.
        seeds = [function([("a", "T")], "published", "a.c", {"T": "int"})]
        seeds.append(function([("a", "T")], "published", "b.c", {"T": "float"}))
        graph = SimpleNamespace(facts=[])
        self.assertTrue(solver._merge_records(seeds, "functions", graph)["f"]["declaration_conflict"] is False)
        # Both spell "T"; the stripped views agree, so the meanings differ but no fact is named.
        self.assertEqual(graph.facts, [])


if __name__ == "__main__":
    unittest.main()
