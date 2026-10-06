"""infer over a cached machine graph equals infer from scratch, for every declared seed set."""

import shutil
import unittest
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
