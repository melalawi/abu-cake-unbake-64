"""infer over a cached machine graph equals infer from scratch, for every declared seed set."""

import json
import shutil
import unittest
from pathlib import Path
from types import SimpleNamespace

from tests.kit import TempCase
from tests.typemap.test_solver import facts
from unbake.cache import Cache
from unbake.typemap import closure
from unbake.typemap.declarations import extract
from unbake.typemap.solver import infer

PROGRAMS = {
    "caller": (0x80001000, [0x0C000800, 0, 0x0C000800, 0, 0x03E00008, 0]),
    "leaf": (0x80002000, [0x03E00008, 0]),
}
SOURCES = (
    "int leaf(int value);",
    "void *leaf(void *value);",
    "int leaf(int value);\nextern int caller(void);",
    "",
)


class CachedClosureTests(TempCase):
    def setUp(self) -> None:
        super().setUp()
        self.project = SimpleNamespace(root=self.root)
        self.mapped = {**facts(PROGRAMS), "shard_sha256": "a" * 64}
        self.cache = Cache(self.root / "cache")
        self.shards = self.root / "build" / "types"

    def solve(self, source: str, cache: Cache | None) -> dict:
        seeds = [extract(source, {"kind": "declared"})] if source else []
        return infer(self.project, self.mapped, seeds, cache=cache, shard_dir=self.shards)

    def test_cached_solves_equal_fresh_solves_in_any_order(self) -> None:
        for order in (SOURCES, tuple(reversed(SOURCES))):
            shutil.rmtree(self.root / "cache", ignore_errors=True)
            for source in (*order, *order):
                with self.subTest(source=source):
                    self.assertEqual(self.solve(source, self.cache), self.solve(source, None))

    def test_a_missing_constraints_shard_is_restored_or_rebuilt(self) -> None:
        expected = self.solve(SOURCES[0], self.cache)
        shard = self.root / expected["constraints"][0]["path"]
        for label, lose_entry in (("restored from the cache", False), ("rebuilt", True)):
            with self.subTest(label):
                shard.unlink()
                if lose_entry:
                    shutil.rmtree(self.root / "cache" / "types-constraints")
                self.assertEqual(self.solve(SOURCES[0], self.cache), expected)
                self.assertTrue(shard.is_file())


class IndexedPointerTests(unittest.TestCase):
    """A loaded value advanced by an untracked index and dereferenced is a pointer to what was read."""

    def access(self, opcode: int, width: int, signedness: bool | None) -> dict:
        return {
            "function": "f",
            "version": "us",
            "instruction": 0x80001000,
            "rom_offset": 0x1000,
            "opcode": opcode,
            "width": width,
            "signedness": signedness,
            "direction": "read",
            "offset": 0,
            "partial": False,
            "base": {"origins": [], "based": ["global:D_800D3C48"], "constant": None},
            "value": None,
        }

    def test_the_based_node_is_seeded_with_a_pointer_to_the_observed_width(self) -> None:
        graph = closure.Constraints()
        body = {"address": 0x80001000}
        closure._access(graph, "f", "us", body, self.access(0x24, 1, False), {"us": {}}, {}, {})
        self.assertEqual(list(graph.seeds["global:D_800D3C48"]), ["unsigned char *"])

    def test_a_word_cell_and_a_pointer_resolve_to_the_pointer_and_mixed_widths_to_void(self) -> None:
        def machine(index: int) -> dict:
            return {"kind": "machine", "instruction": index, "indexed_base": True}

        seeds = {"n": {"int": [machine(1)], "unsigned char *": [machine(2)]}}
        self.assertEqual(closure.resolve(["n"], seeds, {})["type"], "unsigned char *")
        seeds = {"n": {"int": [machine(1)], "unsigned char *": [machine(2)], "short *": [machine(3)]}}
        self.assertEqual(closure.resolve(["n"], seeds, {})["type"], "void *")
        seeds = {"n": {"int *": [{"kind": "machine"}], "float *": [{"kind": "machine"}]}}
        self.assertEqual(closure.resolve(["n"], seeds, {})["state"], "conflict")


class NullPageTests(unittest.TestCase):
    """A constant inside the unmapped first page is a number or NULL, never the address of an object."""

    def test_small_constants_name_no_object_even_when_an_access_recorded_that_address(self) -> None:
        for constant in (0, 4, closure.NULL_PAGE - 1):
            with self.subTest(constant=constant):
                self.assertIsNone(closure.origin_node({"constant": constant}, {constant: ["x"]}))

    def test_a_mapped_address_still_names_its_one_global(self) -> None:
        self.assertEqual(
            closure.origin_node({"constant": 0x80100000}, {0x80100000: ["D_80100000"]}), "address:D_80100000"
        )
        names = {closure.NULL_PAGE: ["D_1"]}
        self.assertEqual(closure.origin_node({"constant": closure.NULL_PAGE}, names), "address:D_1")

    def test_returning_zero_from_two_functions_does_not_join_their_results(self) -> None:
        graph = closure.Constraints()
        for function in ("f", "g"):
            body = {"calls": [], "memory": [], "returns": [{"values": {"r2": {"constant": 0}}}]}
            item = {"versions": {"us": body}}
            signatures = {function: {"registers": []}}
            closure._function_body(
                graph, function, item, signatures, {}, {"us": {0: ["address:us:00000000"]}}, {}, {}, {}, {}
            )
        self.assertNotEqual(graph.root("result:f:r2"), graph.root("result:g:r2"))


class RealNullConstantTests(unittest.TestCase):
    """Three real RageWars functions (us-rev1 map facts) that return or pass 0 and read other globals."""

    def solve(self) -> dict:
        path = Path(__file__).resolve().parents[1] / "fixtures" / "null_constant_facts.json"
        return infer(SimpleNamespace(), json.loads(path.read_text()), [])

    def test_globals_read_as_words_are_not_made_conflicts_by_the_zero_constants_of_other_functions(self) -> None:
        solved = self.solve()["globals"]
        for name in ("D_800CB6F8", "D_80107DF4"):
            with self.subTest(name):
                self.assertEqual((solved[name]["state"], solved[name]["type"]), ("known", "int"))


class PolymorphicCalleeTests(unittest.TestCase):
    def test_a_callee_that_proves_nothing_does_not_join_its_callers(self) -> None:
        graph = closure.Constraints()
        for caller, type_ in (("a", "float"), ("b", "char *")):
            graph.seed(f"global:{caller}", type_, {"kind": "machine"})
            graph.link(f"global:{caller}", "param:f:r4", {"function": caller})
        graph.instantiate()
        self.assertNotEqual(graph.root("global:a"), graph.root("global:b"))

    def test_a_callee_with_one_agreeing_type_joins_its_callers(self) -> None:
        graph = closure.Constraints()
        graph.seed("param:f:r4", "short *", {"kind": "machine"})
        for caller in ("a", "b"):
            graph.seed(f"global:{caller}", "short *", {"kind": "machine"})
            graph.link(f"global:{caller}", "param:f:r4", {"function": caller})
        graph.instantiate()
        self.assertEqual(graph.root("global:a"), graph.root("global:b"))

    def test_real_lookup_callee_and_callers_keep_their_own_types(self) -> None:
        """func_8028FDB4_de looks an object up; three real callers use the result as different objects."""
        path = Path(__file__).resolve().parents[1] / "fixtures" / "polymorphic_callee_facts.json"
        solved = infer(SimpleNamespace(), json.loads(path.read_text()), [])["functions"]
        states = [param["state"] for function in solved.values() for param in function["params"]]
        self.assertNotIn("conflict", states)
        self.assertEqual(
            [(p["state"], p["type"]) for p in solved["func_80260550_de"]["params"]][2], ("known", "void *")
        )
