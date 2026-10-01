"""ROM-only ownership and retained coverage do not require existing C or ELF."""

import struct
import unittest

from unbake.layout.planner import carve, complete_providers
from unbake.layout.rodata_owners import Span
from unbake.layout.split import Function
from unbake.layout.split_analysis import copy_evidence


def function(name: str, start: int, end: int) -> Function:
    return Function("us", name, start, end, 0x80001000 + start, name, "asm", ())


class PlannerTests(unittest.TestCase):
    def test_private_shared_writable_and_unknown_bytes_cover_the_image(self) -> None:
        image = bytearray(0x90)
        struct.pack_into(">6I", image, 0, 0x3C018000, 0xC4203000, 0xC4223004, 0xE4243008, 0x03E00008, 0)
        struct.pack_into(">4I", image, 0x20, 0x3C018000, 0xC4203004, 0x03E00008, 0)
        struct.pack_into(">4I", image, 0x80, 0x3F800000, 0x40000000, 0x40400000, 0xDEADBEEF)
        ff = [function("alpha", 0, 24), function("beta", 0x20, 0x30)]
        constants = carve(bytes(image), ff, [Span(0x80003000, 0x80, 0x90, 0, "copied")])
        self.assertEqual([p["kind"] for p in constants], ["private", "shared", "writable", "unresolved"])
        self.assertEqual(constants[0]["owners"], ["alpha"])
        self.assertEqual(constants[1]["owners"], ["alpha", "beta"])
        self.assertTrue(constants[1]["name"].startswith("rodata/shared/"))
        providers = complete_providers(bytes(image), ff, constants, tuple(ff))
        self.assertEqual(sum(p["end"] - p["start"] for p in providers), len(image))
        self.assertEqual([p["start"] for p in providers[1:]], [p["end"] for p in providers[:-1]])
        self.assertEqual(providers[0]["start"], 0)
        self.assertEqual(providers[-1]["end"], len(image))

    def test_copied_loop_retains_destination_and_complete_extent(self) -> None:
        data = bytearray(0x4000)
        code = [
            0x27BDFFF0,
            0x3C04B000,
            0x24842000,
            0x3C058000,
            0x24A53000,
            0x24060003,
            0x8C820000,
            0xACA20000,
            0x24840004,
            0x24A50004,
            0x14C0FFFB,
            0x24C6FFFF,
        ]
        struct.pack_into(">" + "I" * len(code), data, 0x1000, *code)
        self.assertIn((0x2000, 0x2010, 0x80003000), copy_evidence(bytes(data)))
