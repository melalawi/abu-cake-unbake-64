"""Spelling, the assembled-unit cache, the merge fast path, the pooled render checks and the pooled solve."""

import copy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tests.kit import TempCase
from tests.typemap.test_closure import PROGRAMS, SOURCES
from tests.typemap.test_solver import facts
from unbake import pool
from unbake.config import Held
from unbake.typemap import database, declarations, layers
from unbake.typemap import facts as source_facts
from unbake.typemap.declarations import extract
from unbake.typemap.solver import _merge_records, infer


class SpellingTests(TempCase):
    def test_project_machine_and_outside_paths(self) -> None:
        project = SimpleNamespace(root=self.root / "proj")
        machine = self.root / "machine"
        spell = lambda text: layers.spelling(project, machine, text)  # noqa: E731
        self.assertEqual(spell(str(self.root / "proj/include/a.h")), "include/a.h")
        self.assertEqual(spell(str(self.root / "machine/gcc/include/b.h")), "@machine/gcc/include/b.h")
        self.assertEqual(spell(str(self.root / "proj/include/../src/c.c")), "src/c.c")
        self.assertEqual(spell("include/a.h"), "include/a.h")
        with self.assertRaises(Held) as raised:
            spell("/usr/include/stdio.h")
        self.assertEqual(raised.exception.key, "facts.paths")

    def test_a_moved_project_spells_the_same(self) -> None:
        paths = [self.root / "one" / "include/a.h", self.root / "two" / "include/a.h"]
        spelled = {layers.spelling(SimpleNamespace(root=path.parents[1]), self.root / "m", str(path)) for path in paths}
        self.assertEqual(spelled, {"include/a.h"})


class AssembledCacheTests(TempCase):
    def test_a_hit_returns_the_rows_a_miss_computed(self) -> None:
        source = self.root / "src" / "alpha.c"
        source.parent.mkdir()
        source.write_text("int alpha;\n")
        project = SimpleNamespace(
            root=self.root, include=(), build=self.root / "build", version=lambda v: SimpleNamespace(macros=())
        )
        text = declarations.BOUNDARY + "\nint alpha;\n"
        output = source_facts.Store(None)
        snapshot = source_facts.Snapshot(project)
        content_key = source_facts.unit_key(project, None, source, "us", snapshot)
        group = [(0, content_key, ("alpha", source, "us"))]
        parts = {"us": source_facts._Parts(output, {})}

        def run() -> tuple[list, dict]:
            counts = {"sources": 0, "whole": 0}
            with (
                patch.object(declarations, "source_unit", lambda *args, **named: text),
                patch.object(source_facts.Snapshot, "generated", lambda snapshot: frozenset()),
            ):
                rows = source_facts._source_tasks(
                    project, None, output, parts, {}, group, counts, {}, snapshot, frozenset()
                )
            return rows, counts

        with patch.object(layers, "assemble", wraps=layers.assemble) as assembled:
            miss, counted = run()
            self.assertEqual((assembled.call_count, counted["sources"]), (1, 1))
            hit, counted = run()
            self.assertEqual((assembled.call_count, counted["sources"]), (1, 0))  # nothing assembled or extracted
        self.assertIsNotNone(miss)
        self.assertEqual(hit, miss)


class MergeFastPathTests(TempCase):
    def record(self, type_: str, kind: str = "declared") -> dict:
        return {"state": "known", "type": type_, "provenance": {"kind": kind}}

    def test_a_repeated_object_gives_what_equal_copies_give(self) -> None:
        shared = self.record("int")
        conflicting = self.record("float", "published")
        seeds = [{"globals": {"g": shared}, "aliases": {}} for _ in range(4)]
        seeds.insert(2, {"globals": {"g": conflicting}, "aliases": {}})
        copies = copy.deepcopy(seeds)
        from unbake.typemap.closure import Constraints

        fast, plain = Constraints(), Constraints()
        self.assertEqual(_merge_records(seeds, "globals", fast), _merge_records(copies, "globals", plain))
        self.assertEqual(fast.facts, plain.facts)
        self.assertTrue(fast.facts)


class DropsTests(TempCase):
    def test_pooled_drops_equal_the_serial_drops(self) -> None:
        shared = database._Drops(
            declarations_by_name={"f": "extern int f(void);", "g": "extern int g(void);"},
            components={},
            contracts_by_name={},
            retained_contracts={},
            published_homes=set(),
            typedefs={},
            replacements={},
        )
        items = [
            (Path("one.c"), "int f(int x);\nint g(void);\n"),
            (Path("two.c"), "int h(void) { return 0; }\n"),
        ]
        decisions = [database._source_drops(shared, item) for item in items]
        self.assertEqual([names for _, names, _ in decisions], [{"f"}, set()])
        self.assertEqual(shared.declarations_by_name, {"f": "extern int f(void);", "g": "extern int g(void);"})


class PooledSolveTests(TempCase):
    def solve(self, source: str, pooled: bool) -> dict:
        project = SimpleNamespace(root=self.root)
        seeds = [extract(source, {"kind": "declared"})] if source else []
        shards = self.root / "shards"

        def serial_run(host: object, fn: object, items: list, shared: object = None) -> list:
            return [fn(item) if shared is None else fn(shared, item) for item in items]  # type: ignore[operator]

        with patch.object(pool, "run", serial_run), patch.object(pool, "workers", lambda host: 3):
            return infer(
                project,
                {**facts(PROGRAMS), "shard_sha256": "a" * 64},
                seeds,
                shard_dir=shards,
                policy=SimpleNamespace() if pooled else None,
            )

    def test_abi_and_machine_built_in_pieces_equal_the_serial_ones(self) -> None:
        for source in SOURCES:
            with self.subTest(source=source):
                pooled, serial = self.solve(source, True), self.solve(source, False)
                self.assertEqual(pooled, serial)
                self.assertEqual(pooled["constraints"][0]["sha256"], serial["constraints"][0]["sha256"])
