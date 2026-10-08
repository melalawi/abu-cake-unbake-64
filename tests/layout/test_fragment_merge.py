"""Fragments a split left behind merge into the unit before them; small complete functions stay."""

from __future__ import annotations

import struct
import tempfile
import unittest
from pathlib import Path
from typing import cast

from tests.layout.test_split import ProjectFixture
from unbake.config import Held, Host, Project
from unbake.layout import boundary_ops, fragment_merge, split
from unbake.layout.dead_prelude import Span, Unit
from unbake.layout.fragment_merge import Merge

BASE = 0x80200000
NOP = 0
JR_RA = 0x03E00008
FRAME = 0x27BDFFE8  # addiu sp,sp,-24
POP = 0x27BD0018  # addiu sp,sp,+24
RUNS = [0x24020001, NOP]  # addiu v0,zero,1 ; nop : runs on into the next unit
ENDS = [JR_RA, NOP]
# One fragment per kind seen in the classified small units.
NEEDS_REGISTER = [0x00431021, *ENDS]  # addu v0,v0,v1 ; jr ra ; nop : v1 was never set
NO_RETURN = [0x24420001]  # addiu v0,v0,1 : ends without a return
LEAVES_ROW = [0x10800010, NOP, *ENDS]  # beqz a0,+16 words : out of the row
FPU_CONDITION = [0x45010004, NOP, *ENDS]  # bc1t : on a condition set before the row
SMALL_FUNCTION = [JR_RA, 0x24020001]  # jr ra ; addiu v0,zero,1


def build(*chunks: list[int], kinds: dict[int, str] | None = None) -> tuple[bytes, list[Unit]]:
    image = b""
    units = []
    for index, words in enumerate(chunks):
        kind = (kinds or {}).get(index, "asm")
        units.append(Unit(f"f{index}", len(image), len(image) + len(words) * 4, BASE + len(image), f"f{index}", kind))
        image += struct.pack(f">{len(words)}I", *words)
    return image, units


def jal(address: int) -> int:
    return 0x0C000000 | (address >> 2 & 0x3FFFFFF)


def found(image: bytes, units: list[Unit], data: list[Span] | None = None) -> list[Merge]:
    return fragment_merge.detect("v", image, units, data or [])


class ShapeTests(unittest.TestCase):
    def test_complete_small_function_is_a_function(self) -> None:
        self.assertEqual(fragment_merge.shape(SMALL_FUNCTION)[0], "function")
        self.assertEqual(fragment_merge.shape([FRAME, JR_RA, POP])[0], "function")

    def test_each_evidenced_kind_is_a_fragment_with_its_reason(self) -> None:
        for words, reason in (
            (NEEDS_REGISTER, "undeclared GPR $3"),
            (NO_RETURN, "no complete return"),
            (LEAVES_ROW, "branch leaves the measured row"),
            (FPU_CONDITION, "incoming FPU condition"),
            ([JR_RA], "missing its delay slot"),
            ([POP, JR_RA, NOP], "frame pop before entry prologue"),
        ):
            category, issues = fragment_merge.shape(words)
            self.assertEqual(category, "fragment", words)
            self.assertTrue(any(reason in text for text in issues), (words, issues))

    def test_pops_before_a_later_frame_are_a_stub_for_the_prelude_rule(self) -> None:
        self.assertEqual(fragment_merge.shape([POP, FRAME, JR_RA, POP])[0], "stub")


class DetectTests(unittest.TestCase):
    def test_every_evidenced_fragment_after_a_unit_that_runs_on_merges(self) -> None:
        for words in (NEEDS_REGISTER, NO_RETURN, LEAVES_ROW, FPU_CONDITION):
            image, units = build(RUNS, words, SMALL_FUNCTION)
            (merge,) = found(image, units)
            self.assertEqual((merge.parent, [p.name for p in merge.fragments]), ("f0", ["f1"]), words)
            self.assertEqual(merge.fragments[0].kind, "fallthrough")
            self.assertEqual(merge.merged_size, (len(RUNS) + len(words)) * 4)

    def test_delay_slot_cut_from_its_jump_merges_whatever_its_shape(self) -> None:
        image, units = build([0x24020001, JR_RA], [POP], [FRAME, JR_RA, POP])
        (merge,) = found(image, units)
        self.assertEqual([(p.name, p.kind) for p in merge.fragments], [("f1", "delay-slot")])

    def test_branch_from_the_unit_before_lands_in_a_fragment_after_a_return(self) -> None:
        # beqz a0 -> word 4 ; nop ; jr ra ; nop | addu v0,v0,v1 ; jr ra ; nop
        parent = [0x10800003, NOP, *ENDS]
        image, units = build(parent, NEEDS_REGISTER)
        (merge,) = found(image, units)
        self.assertEqual([(p.name, p.kind) for p in merge.fragments], [("f1", "branch-target")])

    def test_chain_of_fragments_joins_the_first_unit_that_runs_on(self) -> None:
        image, units = build(RUNS, NO_RETURN, NEEDS_REGISTER, SMALL_FUNCTION)
        (merge,) = found(image, units)
        self.assertEqual((merge.parent, [p.name for p in merge.fragments]), ("f0", ["f1", "f2"]))
        self.assertEqual(merge.merged_size, 24)

    def test_small_complete_function_is_never_merged(self) -> None:
        for before in (RUNS, ENDS):
            image, units = build(before, SMALL_FUNCTION, SMALL_FUNCTION)
            self.assertEqual(found(image, units), [], before)

    def test_fragment_after_a_return_that_nothing_reaches_stays(self) -> None:
        image, units = build(ENDS, NEEDS_REGISTER)
        self.assertEqual(found(image, units), [])

    def test_called_fragment_stays(self) -> None:
        caller = [jal(BASE + 8), NOP, *ENDS]
        image, units = build(RUNS, NEEDS_REGISTER, caller)
        self.assertEqual(found(image, units), [])

    def test_pointed_at_fragment_stays(self) -> None:
        image, units = build(RUNS, NEEDS_REGISTER)
        table = struct.pack(">3I", 0, BASE + 8, 0)
        spans = [Span(len(image), len(image) + 12, 0x80300000)]
        self.assertEqual(found(image + table, units, spans), [])

    def test_branch_from_outside_the_run_keeps_a_fragment(self) -> None:
        # f2 branches back into f1: f1 is reachable from beyond the unit that runs into it.
        later = [0x1000FFFE, NOP, *ENDS]
        image, units = build(RUNS, NEEDS_REGISTER, later)
        self.assertEqual(found(image, units), [])

    def test_stub_ahead_of_its_frame_is_left_to_the_prelude_rule(self) -> None:
        image, units = build(RUNS, [POP, FRAME, JR_RA, POP])
        self.assertEqual(found(image, units), [])

    def test_published_c_and_data_rows_are_not_merged_or_merged_into(self) -> None:
        image, units = build(RUNS, NEEDS_REGISTER, kinds={0: "c"})
        self.assertEqual(found(image, units), [])
        image, units = build(RUNS, NEEDS_REGISTER, kinds={1: "c"})
        self.assertEqual(found(image, units), [])

    def test_non_adjacent_units_do_not_join(self) -> None:
        image, units = build(RUNS, [NOP, NOP], NEEDS_REGISTER)
        gap = [units[0], units[2]]
        self.assertEqual(found(image, gap), [])


class ApplyTests(unittest.TestCase):
    """`boundary merge` on a small layout map and symbol table, minus the make proof."""

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.fixture = ProjectFixture(Path(self.directory.name), ("one",))
        self.project = cast(Project, self.fixture)
        self.config = self.fixture.version("one")
        # alpha runs on into beta (a fragment); gamma is a small complete function after it.
        rom = bytes(16) + struct.pack(">7I", *RUNS, *NEEDS_REGISTER, *SMALL_FUNCTION)
        self.config.baserom.write_bytes(rom.ljust(0x60, b"\0"))
        self.fixture.layout(
            "one", [(0x10, "asm", "alpha"), (0x18, "asm", "beta"), (0x24, "asm", "gamma"), (0x2C, "data", "pool")]
        )

    def rows(self) -> list[tuple[str, int, str]]:
        _, _, segments = split.layout(self.config.split)
        return [(r.path, r.start, r.kind) for r in segments[0].rows]

    def test_preview_names_the_fragment_and_writes_nothing(self) -> None:
        before = (self.config.split.read_text(), self.config.symbols.read_text())
        outcome = boundary_ops.merge(self.project, cast(Host, None), [], [], apply=False)
        self.assertTrue(any("alpha: 8 B + beta (12 B fallthrough)" in line for line in outcome.lines))
        self.assertEqual(before, (self.config.split.read_text(), self.config.symbols.read_text()))

    def test_applied_edits_drop_the_row_and_symbol_and_a_rerun_is_a_no_op(self) -> None:
        edits = fragment_merge.plan(self.project, fragment_merge.census(self.project))
        for edit in edits:
            self.assertEqual(edit.path.read_text(), edit.before)
            edit.path.write_text(edit.after)
        self.assertEqual(
            self.rows(),
            [("alpha", 0x10, "asm"), ("gamma", 0x24, "asm"), ("pool", 0x2C, "data")],
        )
        symbols = self.config.symbols.read_text()
        self.assertNotIn("beta", symbols)
        self.assertIn("alpha = 0x80001000;", symbols)
        self.assertIn("gamma = 0x80001014;", symbols)
        (alpha,) = [f for f in split.functions(self.project, "one") if f.name == "alpha"]
        self.assertEqual((alpha.start, alpha.end), (0x10, 0x24))
        self.assertEqual(fragment_merge.census(self.project), [])
        self.assertEqual(fragment_merge.plan(self.project, fragment_merge.census(self.project)), [])

    def test_selecting_an_unproven_name_is_refused_by_name(self) -> None:
        with self.assertRaises(Held) as raised:
            fragment_merge.select(fragment_merge.census(self.project), ["gamma"], [])
        self.assertIn("gamma", str(raised.exception))
        (chosen,) = fragment_merge.select(fragment_merge.census(self.project), ["beta"], [])
        self.assertEqual(chosen.parent, "alpha")


if __name__ == "__main__":
    unittest.main()
