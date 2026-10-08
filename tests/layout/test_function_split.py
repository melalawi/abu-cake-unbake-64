"""A unit that holds two functions is cut where the first ends and a valid entry follows."""

from __future__ import annotations

import struct
import tempfile
import unittest
from pathlib import Path
from typing import cast

from tests.layout.test_dead_prelude import BASE, BODY, NOP, RETURN, build, caller, jal
from tests.layout.test_split import ProjectFixture
from unbake.config import Project
from unbake.layout import function_split, split
from unbake.layout.dead_prelude import Span, Unit

ERET = 0x42000018
RESTORE = [0x40806000, 0x24020001]  # mtc0 ; addiu v0,zero,1
LEAF = [0x24020001, 0x03E00008, NOP]
EARLY = [0x24020001, 0x03E00008, NOP]  # jr ra ; delay slot


def census(image: bytes, units: list[Unit], data: list[Span] | None = None) -> list[function_split.Cut]:
    return function_split.detect("v", image, units, data or [])


class DetectTests(unittest.TestCase):
    def test_eret_terminated_leading_function_before_a_prologue_splits(self) -> None:
        image, units = build([*RETURN], [*RESTORE, ERET, *BODY], caller(BASE + 8))
        (found,) = census(image, units)
        self.assertEqual((found.parent, found.offset, found.ending, found.entry), ("f1", 12, "eret", "prologue"))
        self.assertEqual(found.new_address, BASE + 8 + 12)

    def test_return_then_prologue_splits(self) -> None:
        image, units = build([*RETURN], [*EARLY, *BODY], caller(BASE + 8))
        (found,) = census(image, units)
        self.assertEqual((found.offset, found.ending, found.entry), (12, "return", "prologue"))

    def test_return_then_fragment_without_prologue_or_references_stays(self) -> None:
        image, units = build([*RETURN], [*EARLY, 0x24030002, 0x00431021, *RETURN], caller(BASE + 8))
        self.assertEqual(census(image, units), [])

    def test_leaf_called_from_elsewhere_splits_as_a_referenced_entry(self) -> None:
        image, units = build([*RETURN], [*EARLY, 0x24030002, *RETURN], caller(BASE + 8 + 12))
        (found,) = census(image, units)
        self.assertEqual((found.offset, found.entry), (12, "referenced"))

    def test_jump_to_outside_ends_a_function(self) -> None:
        tail = [0x08000000 | ((BASE + 0x4000) >> 2 & 0x3FFFFFF), NOP]
        image, units = build([*RETURN], [0x24020001, *tail, *BODY], caller(BASE + 8))
        (found,) = census(image, units)
        self.assertEqual((found.offset, found.ending), (12, "jump"))

    def test_jump_table_label_at_the_cut_keeps_the_unit_whole(self) -> None:
        image, units = build([*RETURN], [*EARLY, *BODY], caller(BASE + 8))
        table = struct.pack(">2I", BASE + 8 + 12, BASE + 8 + 16)
        spans = [Span(len(image), len(image) + 8, 0x80300000)]
        self.assertEqual(census(image + table, units, spans), [])

    def test_branch_across_the_cut_keeps_the_unit_whole(self) -> None:
        shared = [0x10000000 | 3, NOP, 0x03E00008, NOP, *BODY]  # b +3 lands past the return
        image, units = build([*RETURN], shared, caller(BASE + 8))
        self.assertEqual(census(image, units), [])


class ApplyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.fixture = ProjectFixture(Path(self.directory.name), ("one",))
        self.project = cast(Project, self.fixture)
        self.config = self.fixture.version("one")
        beta = [*RESTORE, ERET, *BODY]
        alpha = [jal(0x80001010), NOP, 0x03E00008, NOP]
        rom = bytes(16) + struct.pack(f">{4 + len(beta) + 2}I", *alpha, *beta, 0x03E00008, NOP)
        self.config.baserom.write_bytes(rom.ljust(0x60, b"\0"))
        end = 0x20 + len(beta) * 4
        self.fixture.layout(
            "one", [(0x10, "asm", "alpha"), (0x20, "asm", "beta"), (end, "asm", "gamma"), (end + 8, "data", "pool")]
        )

    def rows(self) -> dict[str, tuple[int, str]]:
        _, _, segments = split.layout(self.config.split)
        return {r.path: (r.start, r.kind) for r in segments[0].rows}

    def test_apply_adds_a_row_and_symbol_and_is_idempotent(self) -> None:
        found = function_split.census(self.project)
        self.assertEqual([(c.parent, c.offset) for c in found], [("beta", 12)])
        self.assertEqual(function_split.counts(found)[0], "splits: 1; by end eret=1; by entry prologue=1")
        for edit in function_split.plan(self.project, found):
            self.assertEqual(Path(edit.path).read_text(), edit.before)
            Path(edit.path).write_text(edit.after)
        rows = self.rows()
        self.assertEqual((rows["beta"], rows["func_8000101C"]), ((0x20, "asm"), (0x2C, "asm")))
        self.assertIn("func_8000101C = 0x8000101C; // type:func", self.config.symbols.read_text())
        self.assertEqual(function_split.census(self.project), [])
        self.assertEqual(function_split.plan(self.project, function_split.census(self.project)), [])

    def test_unknown_function_is_refused_by_name(self) -> None:
        from unbake.config import Held

        with self.assertRaises(Held) as raised:
            function_split.select(function_split.census(self.project), ["nope"], [])
        self.assertIn("nope", str(raised.exception.args[0]))


if __name__ == "__main__":
    unittest.main()
