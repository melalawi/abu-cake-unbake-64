"""Actual disassembler nominations include a false function made only of alignment."""

import unittest
from itertools import pairwise
from pathlib import Path

from tests.work.test_shape import SHAPES
from unbake.config import Held
from unbake.layout import planner
from unbake.layout.split import Function


class MeasuredPaddingTests(unittest.TestCase):
    def test_real_disassembler_padding_nomination_is_retained_as_bytes_not_a_function(self):
        image = (Path(__file__).parent / "fixtures/bundled.text").read_bytes()
        # Native Splat output from public setup of these exact bytes.
        cuts = [0, 0x350, 0x95C, 0xF68, 0x12BC, 0x1330, 0x1404, 0x14D0, 0x1544, 0x1550]
        rows = [
            Function("de", f"func_{0x80211120 + a:08X}", a, b, 0x80211120 + a, "fixture", "asm", ())
            for a, b in pairwise(cuts)
        ]
        found = planner.normalized_functions(image, rows, {0: {"loaded-entry"}}, SHAPES, [])
        self.assertEqual(len(found), 8)
        self.assertEqual(sum(f.end - f.start for f in found), 5444)
        providers = planner.complete_providers(image, found, [], tuple(rows), SHAPES)
        self.assertEqual(sum(p["end"] - p["start"] for p in providers), 5456)
        self.assertEqual(
            sum(
                p["end"] - p["start"]
                for p in providers
                if p["evidence"].get("classification") == "proved compiler alignment"
            ),
            12,
        )
        with self.assertRaises(Held):
            planner.normalized_functions(image, rows, {0: {"loaded-entry"}, 0x1544: {"jal-target"}}, SHAPES, [])

    def test_alignment_at_loaded_extent_is_not_hidden_in_unmapped_cartridge_bytes(self):
        text = (Path(__file__).parent / "fixtures/bundled.text").read_bytes()
        image = text + bytes(64)
        region = Function("de", "region", 0, len(text), 0x80211120, "region", "asm", ())
        bodies = planner.normalized_functions(image, [region], {0: {"loaded-entry"}}, SHAPES, [])
        providers = planner.complete_providers(image, bodies, [], (region,), SHAPES)
        padding = [p for p in providers if p["evidence"].get("classification") == "proved compiler alignment"]
        self.assertEqual([(p["start"], p["end"], p["address"]) for p in padding], [(5444, 5456, 0x80212664)])
        self.assertEqual(providers[-1]["address"], None)
        self.assertEqual((providers[-1]["start"], providers[-1]["end"]), (5456, len(image)))
        self.assertEqual(sum(p["end"] - p["start"] for p in providers), len(image))
