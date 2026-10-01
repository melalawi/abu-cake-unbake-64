"""Compiler literals can occupy a shared pool without source alignment tricks."""

import struct
import tempfile
import unittest
from pathlib import Path

from tests.decomp.test_trial import assemble
from unbake.project_tools.elf import Object
from unbake.project_tools.layout import resident
from unbake.project_tools.literal_layout import arrange
from unbake.project_tools.rodata import placement, relocated


class LiteralLayoutTests(unittest.TestCase):
    def test_shared_duplicates_and_misaligned_table_prove_each_word(self) -> None:
        for biased in (False, True):
            with self.subTest(biased=biased), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                obj = Object(
                    assemble(
                        root,
                        "pool",
                        (
                            ".set noreorder\n.text\n.globl alpha\n.type alpha,@function\nalpha:\n"
                            "lui $at,%hi(literal)\nlwc1 $f0,%lo(literal)($at)\n"
                            "lui $at,%hi(literal)\nlwc1 $f2,%lo(literal)($at)\n"
                            "lui $at,%hi(table)\naddiu $v0,$at,%lo(table)\n"
                            "case:\njr $ra\nnop\n.size alpha,.-alpha\n"
                            ".section .rdata\nliteral:\n.word 0x3f800000\n.align 3\ntable:\n.word case,case\n"
                        ),
                    )
                )
                target = [0x3C018000, 0xC4201004, 0x3C018000, 0xC422100C, 0x3C018000, 0x24221010, 0x03E00008, 0]
                image = bytearray(0x140)
                image[0x40:0x60] = struct.pack(">8I", *target)
                pointer = 0x2018 if biased else 0x80002018
                image[0x100:0x118] = struct.pack(
                    ">6I", 0x4F000000, 0x3F800000, 0x12345678, 0x3F800000, pointer, pointer
                )
                interval = dict(address=0x80002000, start=0x40, end=0x60)
                mapping = dict(address=0x80001000, start=0x100, end=0x118, table_entry_bias=0x80000000 if biased else 0)

                def read_memory(address: int, size: int, image: bytearray = image) -> bytes:
                    return bytes(image[0x100 + address - 0x80001000 :][:size])

                before = obj.path.read_bytes()
                image[0x104:0x108] = bytes(4)
                with self.assertRaisesRegex(ValueError, "bytes: disagree"):
                    arrange(
                        obj,
                        ".rdata",
                        {i * 4: w for i, w in enumerate(target)},
                        0x80002000,
                        read_memory,
                    )
                self.assertEqual(obj.path.read_bytes(), before)
                image[0x104:0x108] = struct.pack(">I", 0x3F800000)
                base = resident(obj, interval, bytes(image), ".rdata", [mapping])
                self.assertEqual(base, 0x80001004)
                rebuilt = Object(obj.path)
                self.assertEqual(placement(rebuilt, ".rdata", {i * 4: w for i, w in enumerate(target)}), (base, 0))
                data = relocated(rebuilt, ".rdata", 0x80002000)
                self.assertEqual(
                    struct.unpack(">5I", data), (0x3F800000, 0x12345678, 0x3F800000, 0x80002018, 0x80002018)
                )
                self.assertEqual(resident(rebuilt, interval, bytes(image), ".rdata", [mapping]), base)
