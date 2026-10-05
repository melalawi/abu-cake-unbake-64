"""infer over a cached machine graph equals infer from scratch, for every declared seed set."""

import shutil
from types import SimpleNamespace

from tests.kit import TempCase
from tests.typemap.test_solver import facts
from unbake.cache import Cache
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
