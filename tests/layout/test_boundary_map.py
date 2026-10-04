"""Bulk boundary maps keep byte coverage and roll back on cartridge failures."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from typing import cast

from tests.layout.test_split import ProjectFixture, fake_build
from unbake.layout import boundary_map, split
from unbake.config import Held, Host, Project


class BoundaryMapTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.fixture = ProjectFixture(Path(self.directory.name), ("one", "two", "three", "four", "five"))
        self.project = cast(Project, self.fixture)
        self.policy = cast(Host, self.fixture.policy)

    def change(self, version: str, function: str, action: str, neighbour: str = "") -> boundary_map.Change:
        config = self.fixture.version(version)
        _, _, segments = split.layout(config.split)
        row = next(row for segment in segments for row in segment.rows if row.path == function)
        digest = hashlib.sha256(config.baserom.read_bytes()[row.start : split.end(row)]).hexdigest()
        return boundary_map.Change(version, function, action, digest, "measured instruction/caller evidence", neighbour)

    def test_all_versions_are_planned_and_built_once(self) -> None:
        changes = [self.change(v, "beta", "merge", "alpha") for v in self.project.versions]
        self.fixture.generations()
        with fake_build(self.fixture) as calls:
            results = boundary_map.apply(self.project, self.policy, changes)
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(results), 5)
        for version in self.project.versions:
            _, _, segments = split.layout(self.fixture.version(version).split)
            self.assertEqual(
                [(r.path, r.start) for r in segments[0].rows], [("alpha", 0x10), ("gamma", 0x38), ("pool", 0x40)]
            )

    def test_rom_failure_names_changes_and_rolls_back_every_version(self) -> None:
        changes = [self.change(v, "beta", "data") for v in self.project.versions]
        before = {v: self.fixture.version(v).split.read_bytes() for v in self.project.versions}
        self.fixture.generations()
        with fake_build(self.fixture, failing="four"), self.assertRaisesRegex(Held, "four:beta.*rolled back"):
            boundary_map.apply(self.project, self.policy, changes)
        for version in self.project.versions:
            self.assertEqual(self.fixture.version(version).split.read_bytes(), before[version])
            self.assertEqual(self.fixture.build_link(version).readlink().name, version + ".0")

    def test_stale_byte_pin_refuses_named_change_before_any_write(self) -> None:
        change = self.change("one", "beta", "data")
        self.fixture.version("one").baserom.write_bytes(bytes(128))
        before = self.fixture.version("one").split.read_bytes()
        with self.assertRaisesRegex(Held, "beta: ROM bytes differ"):
            boundary_map.plan(self.project, [change])
        self.assertEqual(self.fixture.version("one").split.read_bytes(), before)

    def test_nonadjacent_and_cross_segment_merges_are_refused(self) -> None:
        with self.assertRaisesRegex(Held, "gamma: nonadjacent neighbour alpha"):
            boundary_map.plan(self.project, [self.change("one", "gamma", "merge", "alpha")])

    def test_multiple_fragments_can_merge_to_same_owner(self) -> None:
        edits = boundary_map.plan(self.project, [self.change("one", n, "merge", "alpha") for n in ("beta", "gamma")])
        _, _, segments = split.parse_layout(edits[0].path, edits[0].after)
        self.assertEqual(
            [(r.path, r.start, split.end(r)) for r in segments[0].rows], [("alpha", 0x10, 0x40), ("pool", 0x40, 0x60)]
        )

    def test_head_merge_preserves_assembly_owner_symbol_and_rom_coverage(self) -> None:
        edits = boundary_map.plan(self.project, [self.change("one", "alpha", "merge", "beta")])
        _, _, segments = split.parse_layout(edits[0].path, edits[0].after)
        self.assertEqual(segments[0].rows[0].path, "beta")
        self.assertEqual(segments[0].rows[0].start, 0x10)
        self.assertIn("beta = 0x80001010", self.fixture.version("one").symbols.read_text())

    def test_partial_tail_retyping_keeps_function_start(self) -> None:
        from dataclasses import replace

        change = replace(self.change("one", "beta", "data"), start=0x30)
        edits = boundary_map.plan(self.project, [change])
        _, _, segments = split.parse_layout(edits[0].path, edits[0].after)
        self.assertEqual(
            [(r.start, r.kind) for r in segments[0].rows],
            [(0x10, "asm"), (0x20, "asm"), (0x30, "data"), (0x38, "asm"), (0x40, "data")],
        )

    def test_tail_for_one_owner_does_not_move_the_next_owner(self) -> None:
        self.fixture.layout(
            "one",
            [
                (0x10, "asm", "alpha"),
                (0x20, "asm", "beta"),
                (0x30, "asm", "gamma"),
                (0x38, "asm", "delta"),
                (0x40, "data", "pool"),
            ],
        )
        edits = boundary_map.plan(
            self.project, [self.change("one", "beta", "merge", "alpha"), self.change("one", "delta", "merge", "gamma")]
        )
        _, _, segments = split.parse_layout(edits[0].path, edits[0].after)
        self.assertEqual(
            [(r.path, r.start) for r in segments[0].rows], [("alpha", 0x10), ("gamma", 0x30), ("pool", 0x40)]
        )

    def test_real_entry_after_stale_prefix_keeps_code_bytes(self) -> None:
        from dataclasses import replace

        change = replace(self.change("one", "beta", "entry", "leaf"), start=0x24)
        edits = boundary_map.plan(self.project, [change])
        _, _, segments = split.parse_layout(edits[0].path, edits[0].after)
        rows = segments[0].rows
        self.assertEqual(
            [(r.path, r.start, r.kind) for r in rows[1:3]], [("beta_prefix", 0x20, "data"), ("leaf", 0x24, "asm")]
        )
        self.assertEqual(sum(split.end(r) - r.start for r in rows), 0x50)

    def test_referenced_data_can_be_restored_to_code_with_the_same_bytes(self) -> None:
        edits = boundary_map.plan(self.project, [self.change("one", "pool", "code")])
        _, _, segments = split.parse_layout(edits[0].path, edits[0].after)
        self.assertEqual(segments[0].rows[-1].kind, "asm")
        self.assertEqual(segments[0].rows[-1].start, 0x40)
        self.assertEqual(split.end(segments[0].rows[-1]), 0x60)

    def test_entry_rename_keeps_the_old_symbol_as_a_real_address_alias(self) -> None:
        from dataclasses import replace

        change = replace(self.change("one", "beta", "entry", "leaf"), start=0x20)
        edits = boundary_map.plan(self.project, [change])
        layout = next(e for e in edits if e.path.suffix == ".yaml")
        _, _, segments = split.parse_layout(layout.path, layout.after)
        self.assertEqual(segments[0].rows[1].path, "leaf")
        symbols = next(e for e in edits if e.path.name == "symbol_addrs.txt")
        self.assertIn("beta = 0x80001010", symbols.after)
        self.assertIn("leaf = 0x80001010", symbols.after)

    def test_invalid_map_records_are_refused(self) -> None:
        path = Path(self.directory.name) / "map.json"
        path.write_text(json.dumps([{"function": "beta", "action": "data"}]))
        with self.assertRaisesRegex(Held, "missing string fields"):
            boundary_map.read(path)
        change = self.change("one", "beta", "data")
        with self.assertRaisesRegex(Held, "beta: duplicate change"):
            boundary_map.plan(self.project, [change, change])
