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
