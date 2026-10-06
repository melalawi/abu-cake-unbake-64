"""Executable ownership uses exact paths, not return counts or nearby restores."""

import struct
import unittest
from pathlib import Path

from tests.work.test_shape import SHAPES
from unbake.config import Held
from unbake.layout import boundary, planner
from unbake.layout.split import Function

TARGET = SHAPES["gcc-2.8.1-sn64"]


def prove(text, *, tables=None, known=(), target=TARGET):
    values = [int(word, 16) for word in text.split()]
    return boundary.evidence(
        {i * 4: w for i, w in enumerate(values)},
        0,
        len(values) * 4,
        0x80001000,
        {"native-fixture-entry"},
        set(known),
        target,
        tables,
    )


class BoundaryProofTests(unittest.TestCase):
    def test_native_guarded_division_and_multiple_exits_close(self):
        division = (
            "0085001a 00001012 14a00002 00000000 0007000d 2401ffff 14a10004 "
            "3c018000 14810002 00000000 0006000d 03e00008 00000000"
        )
        multiple = "10800003 00000000 03e00008 00801025 03e00008 00001025"
        for text in (division, multiple):
            verdict = prove(text)
            self.assertTrue(verdict.proven, verdict.unproven)
        self.assertFalse(prove("0007000d 03e00008 00000000").proven)

    def test_exact_balance_and_owned_ra_are_required(self):
        cases = {
            "mismatched allocation": "27bdffe0 afbf001c 8fbf001c 03e00008 27bd0010",
            "delayed opening": "00801025 27bdffe0 03e00008 00000000",
            "cumulative imbalance": "27bdffe0 27bdfff0 27bd0020 03e00008 00000000",
            "call clobbered ra": "27bdffe0 afbf001c 0c000800 00000000 03e00008 27bd0020",
            "retired frame across call": "27bdffe0 afbf001c 27bd0020 0c000800 00000000 8fbffffc 03e00008 00000000",
            "delay transfers": "03e00008 08000400",
        }
        for label, text in cases.items():
            with self.subTest(label=label):
                self.assertFalse(prove(text).proven)
        self.assertTrue(prove("27bdffe0 afbf001c afa40018 0c000800 00000000 8fbf001c 03e00008 27bd0020").proven)

    def test_tail_restore_before_jump_and_in_delay_share_the_same_state(self):
        before = "27bdffe0 afbf001c 8fbf001c 27bd0020 08000800 00000000"
        delayed = "27bdffe0 afbf001c 8fbf001c 08000800 27bd0020"
        for text in (before, delayed):
            verdict = prove(text, known=(0x1000,))
            self.assertTrue(verdict.proven, verdict.unproven)
            self.assertIn("tail-call:0x80002000", verdict.tags)

    def test_likely_annuls_stack_adjustment_on_the_untaken_path(self):
        text = "27bdffe0 50800003 27bd0020 03e00008 27bd0020 03e00008 00000000"
        verdict = prove(text)
        self.assertTrue(verdict.proven, verdict.unproven)
        unsafe = text.replace("50800003", "10800003")
        self.assertFalse(prove(unsafe).proven)

    def test_table_edges_and_dead_zero_islands_require_exact_ownership(self):
        text = "00804025 01000008 00000000 00000000 03e00008 00000000"
        self.assertFalse(prove(text).proven)
        self.assertTrue(prove(text, tables={4: (16,)}).proven)
        self.assertFalse(prove(text, tables={4: (24,)}).proven)
        self.assertFalse(prove(text.replace("00000000 03e00008", "24020001 03e00008"), tables={4: (16,)}).proven)

    def test_real_eight_body_fixture_preserves_every_byte(self):
        image = (Path(__file__).parent / "fixtures/bundled.text").read_bytes()
        self.assertEqual(len(image), 5456)
        candidate = Function("de", "aggregate", 0, len(image), 0x80211120, "aggregate", "asm", ())
        bodies = planner.normalized_functions(image, [candidate], {0: {"disassembler-entry"}}, SHAPES, [])
        self.assertEqual(len(bodies), 8)
        self.assertEqual(sum(row.end - row.start for row in bodies), 5444)
        retained = planner.complete_providers(image, bodies, [], (candidate,), SHAPES)
        self.assertEqual(sum(row["end"] - row["start"] for row in retained), len(image))
        padding = [row for row in retained if row["evidence"].get("classification") == "proved compiler alignment"]
        self.assertEqual(sum(row["end"] - row["start"] for row in padding), 12)

    def test_unknown_partition_and_unproved_publication_fail_by_name(self):
        image = struct.pack(">4I", 0x03E00008, 0, 0x24020001, 0x24020002)
        candidate = Function("us", "unknown", 0, len(image), 0x80001000, "unknown", "asm", ())
        with self.assertRaisesRegex(Held, "layout.partition: us unknown"):
            planner.normalized_functions(image, [candidate], {0: {"disassembler-entry"}}, SHAPES, [])
        with self.assertRaisesRegex(Held, "required proved executable partition"):
            planner.require_boundaries({"versions": {"us": {"functions": [{"name": "unknown", "evidence": {}}]}}})
