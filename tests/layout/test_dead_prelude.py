"""Dead preludes, jump thunks and pre-frame stubs split off a function; compiler hoists stay."""

from __future__ import annotations

import struct
import tempfile
import unittest
from pathlib import Path
from typing import cast

from tests.layout.test_split import ProjectFixture
from unbake.config import Project
from unbake.layout import dead_prelude, split
from unbake.layout.dead_prelude import Prelude, Span, Unit

BASE = 0x80200000
RETURN = [0x03E00008, 0x27BD0010]  # jr ra ; addiu sp,sp,+16
FRAME = 0x27BDFF88  # addiu sp,sp,-120
NOP = 0
# Real shapes: `lui s2 / addiu s2 / sw ra,28(sp)` ahead of the frame, a `j` overlay thunk, a lone dead `sll`.
DEAD_PRELUDE = [0x3C128014, 0x265212A4, 0xAFBF001C]
THUNK = [0x08100000 | 0x2C95, NOP]
BODY = [FRAME, 0xAFBF0044, 0x00001021, 0x8FBF0044, 0x03E00008, 0x27BD0078]


def build(*chunks: list[int]) -> tuple[bytes, list[Unit]]:
    image = b""
    units = []
    for index, words in enumerate(chunks):
        units.append(Unit(f"f{index}", len(image), len(image) + len(words) * 4, BASE + len(image), f"f{index}"))
        image += struct.pack(f">{len(words)}I", *words)
    return image, units


def jal(address: int) -> int:
    return 0x0C000000 | (address >> 2 & 0x3FFFFFF)


def caller(target: int) -> list[int]:
    return [jal(target), NOP, *RETURN]


def census(image: bytes, units: list[Unit], data: list[Span] | None = None) -> list[Prelude]:
    return dead_prelude.detect("v", image, units, data or [])


class DetectTests(unittest.TestCase):
    def test_every_reference_past_entry_makes_the_leading_bytes_dead(self) -> None:
        image, units = build([*RETURN], [*DEAD_PRELUDE, *BODY], caller(BASE + 8 + 12))
        (found,) = census(image, units)
        self.assertEqual((found.name, found.size, found.shape, found.references), ("f1", 12, "dead", ("jal",)))
        self.assertEqual((found.new_start, found.new_address), (8 + 12, BASE + 8 + 12))

    def test_previous_unit_falling_through_keeps_the_prelude_live(self) -> None:
        image, units = build([0x24020001, NOP], [*DEAD_PRELUDE, *BODY], caller(BASE + 8 + 12))
        self.assertEqual([item for item in census(image, units) if item.shape == "dead"], [])

    def test_branch_into_the_prelude_keeps_it_live(self) -> None:
        looping = [*DEAD_PRELUDE, FRAME, 0x10000000 | (-3 & 0xFFFF), NOP, *RETURN]
        image, units = build([*RETURN], looping, caller(BASE + 8 + 12))
        self.assertEqual(census(image, units), [])

    def test_references_at_the_entry_make_a_stub_that_keeps_the_symbol(self) -> None:
        image, units = build([*RETURN], [*DEAD_PRELUDE, *BODY], caller(BASE + 8))
        (found,) = census(image, units)
        self.assertEqual((found.shape, found.size, found.references), ("stub", 12, ("stub", "jal")))

    def test_leading_jump_thunk_is_split_before_the_real_entry(self) -> None:
        image, units = build([*RETURN], [*THUNK, *BODY], caller(BASE + 8))
        (found,) = census(image, units)
        self.assertEqual((found.shape, found.size), ("thunk", 8))

    def test_thunk_into_its_own_body_is_not_a_thunk(self) -> None:
        inside = [0x08000000 | ((BASE + 8 + 8) >> 2 & 0x3FFFFFF), NOP, *BODY]
        image, units = build([*RETURN], inside, caller(BASE + 8))
        self.assertEqual(census(image, units), [])

    def test_lone_dead_instruction_before_the_frame_is_a_stub(self) -> None:
        # sll v0,a1,3 ; addiu sp ; ... v0 is rewritten before any read.
        image, units = build([*RETURN], [0x00051040, *BODY], caller(BASE + 8))
        (found,) = census(image, units)
        self.assertEqual((found.shape, found.size), ("stub", 4))

    def test_compiler_hoisted_load_that_the_body_reads_is_not_split(self) -> None:
        hoisted = [0x3C028014, 0x8C42B364, FRAME, 0xAFBF0044, 0x00402021, 0x8FBF0044, 0x03E00008, 0x27BD0078]
        image, units = build([*RETURN], hoisted, caller(BASE + 8))
        self.assertEqual(census(image, units), [])

    def test_jump_table_words_are_not_references(self) -> None:
        # Two adjacent pointers into one function are case labels, not callers.
        image, units = build([*RETURN], [FRAME, NOP, NOP, *RETURN, *RETURN])
        table = struct.pack(">2I", BASE + 8 + 8, BASE + 8 + 12)
        spans = [Span(len(image), len(image) + 8, 0x80300000)]
        self.assertEqual(census(image + table, units, spans), [])

    def test_function_pointer_table_entry_past_the_entry_counts_once(self) -> None:
        image, units = build([*RETURN], [*DEAD_PRELUDE, *BODY])
        table = struct.pack(">3I", 0, BASE + 8 + 12, 0)
        spans = [Span(len(image), len(image) + 12, 0x80300000)]
        (found,) = census(image + table, units, spans)
        self.assertEqual((found.shape, found.references), ("dead", ("pointer",)))

    def test_sibling_version_entry_is_evidence_without_local_references(self) -> None:
        image, units = build([*RETURN], [*DEAD_PRELUDE, *BODY])
        hinted = [Unit(u.name, u.start, u.end, u.address, u.path, u.kind, ((12, "sibling-entry"),)) for u in units]
        (found,) = [item for item in census(image, hinted) if item.shape == "dead"]
        self.assertEqual(found.references, ("sibling-entry",))

    def test_c_rows_are_never_split(self) -> None:
        image, units = build([*RETURN], [*DEAD_PRELUDE, *BODY], caller(BASE + 8 + 12))
        published = [Unit(u.name, u.start, u.end, u.address, u.path, "c") for u in units]
        self.assertEqual(census(image, published), [])


class PlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.fixture = ProjectFixture(Path(self.directory.name), ("one", "two", "three", "four", "five"))
        self.project = cast(Project, self.fixture)

    def edits(self, shape: str) -> dict[str, str]:
        beta = next(f for f in split.functions(self.project, "one") if f.name == "beta")
        self.address = beta.address
        item = Prelude("one", "beta", "beta", beta.start, beta.address, 8, beta.end - beta.start, ("jal",), shape)
        return {Path(edit.path).name: edit.after for edit in dead_prelude.plan(self.project, [item])}

    def test_dead_prelude_becomes_data_and_the_symbol_moves(self) -> None:
        edits = self.edits("dead")
        config = self.fixture.version("one")
        _, _, segments = split.parse_layout(config.split, edits[config.split.name])
        start = next(f for f in split.functions(self.project, "one") if f.name == "beta").start
        self.assertEqual(
            [(r.path, r.start, r.kind) for r in segments[0].rows if r.path.startswith("beta")],
            [("beta_prelude", start, "data"), ("beta", start + 8, "asm")],
        )
        self.assertIn(f"beta = 0x{self.address + 8:08X}", edits[config.symbols.name])
        self.assertNotIn("beta_dead", edits[config.symbols.name])

    def test_stub_keeps_the_entry_symbol_on_the_stub_unit(self) -> None:
        edits = self.edits("stub")
        config = self.fixture.version("one")
        self.assertIn(f"beta = 0x{self.address + 8:08X}", edits[config.symbols.name])
        self.assertIn(f"beta_stub = 0x{self.address:08X}; // type:func", edits[config.symbols.name])
        _, _, segments = split.parse_layout(config.split, edits[config.split.name])
        start = next(f for f in split.functions(self.project, "one") if f.name == "beta").start
        self.assertIn(("beta_stub", start, "data"), [(r.path, r.start, r.kind) for r in segments[0].rows])


if __name__ == "__main__":
    unittest.main()
