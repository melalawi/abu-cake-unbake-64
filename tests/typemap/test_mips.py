"""Measured provenance survives joins, spills and MIPS delay slots."""

import unittest

from unbake.typemap.mips import Analysis


def analyze(words: list[int], targets: dict[int, str] | None = None) -> dict:
    return Analysis("caller", "us", 0x80001000, 0x40, words, targets or {}, {}).run()


class MipsFactsTests(unittest.TestCase):
    def test_delay_slot_supplies_actual_argument_and_return_use(self) -> None:
        result = analyze([0x00808021, 0x0C000800, 0x02002021, 0x00401821, 0x03E00008, 0], {0x80002000: "leaf"})
        call = result["calls"][0]
        self.assertEqual(call["arguments"]["r4"]["origins"], [{"id": "param:caller:r4", "offset": 0}])
        self.assertEqual(call["return_use"], [0x8000100C])
        self.assertEqual(call["rom_offset"], 0x44)

    def test_branch_join_does_not_choose_one_base(self) -> None:
        result = analyze([0x10800003, 0, 0x00A08021, 0x10000002, 0x00C08021, 0, 0x8E020004, 0x03E00008, 0])
        origins = result["memory"][0]["base"]["origins"]
        self.assertEqual({row["id"] for row in origins}, {"param:caller:r6"})
        # Both paths execute the unconditional branch's slot, which sets s0=a2.
        self.assertEqual(result["memory"][0]["width"], 4)

    def test_different_join_values_are_retained_as_ambiguous(self) -> None:
        result = analyze([0x10800004, 0, 0x00A08021, 0x10000002, 0, 0x00C08021, 0x8E020004, 0x03E00008, 0])
        origins = result["memory"][0]["base"]["origins"]
        self.assertEqual({row["id"] for row in origins}, {"param:caller:r5", "param:caller:r6"})

    def test_stack_spill_reload_retains_base_but_partial_overwrite_invalidates_it(self) -> None:
        words = [0x27BDFFF0, 0xAFA40000, 0x8FA80000, 0x81020003, 0x03E00008, 0]
        access = analyze(words)["memory"][2]
        self.assertEqual(access["base"]["origins"][0]["id"], "param:caller:r4")
        self.assertEqual((access["width"], access["signedness"]), (1, True))
        changed = analyze([*words[:2], 0xA3A00001, *words[2:]])["memory"][3]
        self.assertTrue(changed["base"]["unknown"])

    def test_global_address_is_measured_and_call_clobber_is_unknown(self) -> None:
        result = analyze(
            [0x3C088012, 0x25083450, 0x8D020004, 0x0C000800, 0, 0x8D030004, 0x03E00008, 0], {0x80002000: "leaf"}
        )
        self.assertEqual(result["memory"][0]["base"]["constant"], 0x80123450)
        self.assertTrue(result["memory"][1]["base"]["unknown"])

    def test_loop_widens_offset_provenance_instead_of_choosing_a_size(self) -> None:
        result = analyze([0x24840004, 0x1480FFFE, 0x8C820000, 0x03E00008, 0])
        self.assertTrue(result["memory"][0]["base"]["unknown"])

    def test_width_signedness_and_partial_access_are_separate_from_types(self) -> None:
        result = analyze([0x90820001, 0x84830002, 0x88880003, 0xC4800004, 0xE4800008, 0x03E00008, 0])
        self.assertEqual(
            [(a["width"], a["signedness"]) for a in result["memory"]],
            [(1, False), (2, True), (4, None), (4, None), (4, None)],
        )
        self.assertTrue(result["memory"][2]["partial"])
        self.assertEqual(result["memory"][-1]["direction"], "write")
        self.assertNotIn("type", result["memory"][0])

    def test_unreachable_memory_is_still_visible_with_unknown_base(self) -> None:
        result = analyze([0x03E00008, 0, 0x8C820004])
        self.assertEqual(len(result["memory"]), 1)
        self.assertTrue(result["memory"][0]["base"]["unknown"])


if __name__ == "__main__":
    unittest.main()
