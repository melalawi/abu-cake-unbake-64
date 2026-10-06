"""Unchanged type facts reuse closure while evidence hashes remain current and distinct."""

from types import SimpleNamespace
from unittest.mock import MagicMock

from tests.kit import TempCase
from unbake.cache import Cache
from unbake.typemap import inference_cache


class InferenceCacheTests(TempCase):
    def setUp(self):
        super().setUp()
        self.project = SimpleNamespace(root=self.root)
        self.cache = Cache(self.root / "cache")

    def seed(self, digest="one", type_="int", kind="proven"):
        return {
            "aliases": {},
            "functions": {"f": {"return": type_, "provenance": {"kind": kind, "source": "src/f.c", "sha256": digest}}},
        }

    def test_body_edit_reuses_inference_and_rebinds_nested_receipts(self):
        first = self.seed()
        receipt = first["functions"]["f"]["provenance"]
        compute = MagicMock(return_value={"functions": {"f": {"provenance": [receipt]}}, "constraints": []})
        original, original_key, original_receipts = inference_cache.infer(
            self.project, self.cache, ["map"], [first], compute
        )
        changed = self.seed("two")
        result, result_key, result_receipts = inference_cache.infer(
            self.project, self.cache, ["map"], [changed], compute
        )
        self.assertEqual(compute.call_count, 1)
        self.assertEqual(original_key, result_key)
        self.assertNotEqual(original_receipts, result_receipts)
        self.assertEqual(original["functions"]["f"]["provenance"][0]["sha256"], "one")
        self.assertEqual(result["functions"]["f"]["provenance"][0]["sha256"], "two")

    def test_type_confidence_inventory_map_and_nonreceipt_hashes_are_inputs(self):
        compute = MagicMock(return_value={})
        inference_cache.infer(self.project, self.cache, ["map"], [self.seed()], compute)
        for parts, seeds in (
            (["map"], [self.seed(type_="float")]),
            (["map"], [self.seed(kind="declared")]),
            (["new map"], [self.seed()]),
            (["map"], [self.seed(), self.seed("other")]),
            (["map"], [{**self.seed(), "sha256": "unrelated content pin"}]),
        ):
            before = compute.call_count
            inference_cache.infer(self.project, self.cache, parts, seeds, compute)
            self.assertEqual(compute.call_count, before + 1)

    def test_distinct_receipts_and_their_equality_partition_are_preserved(self):
        seeds = [self.seed("a"), self.seed("b")]
        receipts = [seed["functions"]["f"]["provenance"] for seed in seeds]
        compute = MagicMock(return_value={"provenance": receipts})
        first, first_key, _ = inference_cache.infer(self.project, self.cache, [], seeds, compute)
        changed = [self.seed("c"), self.seed("d")]
        second, second_key, _ = inference_cache.infer(self.project, self.cache, [], changed, compute)
        self.assertEqual(first_key, second_key)
        self.assertEqual([row["sha256"] for row in first["provenance"]], ["a", "b"])
        self.assertEqual([row["sha256"] for row in second["provenance"]], ["c", "d"])
        _, collapsed, _ = inference_cache.infer(self.project, self.cache, [], [self.seed("c"), self.seed("c")], compute)
        self.assertNotEqual(collapsed, first_key)
        self.assertEqual(compute.call_count, 2)

    def test_decoded_mutation_cannot_poison_later_hits(self):
        compute = MagicMock(return_value={"functions": {"f": {"return": "int"}}})
        first, _, _ = inference_cache.infer(self.project, self.cache, [], [self.seed()], compute)
        first["functions"]["f"]["return"] = "float"
        second, _, _ = inference_cache.infer(self.project, self.cache, [], [self.seed()], compute)
        self.assertEqual(second["functions"]["f"]["return"], "int")

    def test_cached_solution_equals_a_fresh_solve_with_new_evidence_stamps(self):
        from tests.typemap.test_solver import facts
        from unbake.typemap import declarations, solver

        mapped = facts({"leaf": (0x80001000, [0x24020001, 0x03E00008, 0])})

        def seeds(stamp):
            return [declarations.extract("int leaf(int arg); extern int cell;", {"kind": "declared", "sha256": stamp})]

        original = seeds("a")
        compute = MagicMock(side_effect=lambda: solver.infer(self.project, mapped, original))
        inference_cache.infer(self.project, self.cache, ["mapped"], original, compute)
        changed = seeds("b")
        actual, _, _ = inference_cache.infer(self.project, self.cache, ["mapped"], changed, compute)
        expected = solver.infer(self.project, mapped, changed)
        self.assertEqual(actual, expected)
        self.assertEqual(compute.call_count, 1)

    def test_shared_component_lists_stay_shared_in_the_cached_graph(self):
        users = ["a", "b"]
        compute = MagicMock(return_value={"nodes": {"a": {"users": users}, "b": {"users": users}}})
        for _ in range(2):
            result, _, _ = inference_cache.infer(self.project, self.cache, [], [self.seed()], compute)
            self.assertIs(result["nodes"]["a"]["users"], result["nodes"]["b"]["users"])
