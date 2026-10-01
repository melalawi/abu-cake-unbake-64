"""Disjoint private pools replace exactly one load selector per slice."""

import struct
import tempfile
import unittest
from pathlib import Path

from tests.decomp.support import assemble
from unbake.project_tools.elf import Object
from unbake.project_tools.extract import pool_rows, unit_ranges
from unbake.project_tools.layout import transfer_private, transfer_selectors


class PoolSliceTests(unittest.TestCase):
    def test_structural_rows_keep_disjoint_owner_and_shared_storage(self) -> None:
        yaml = (
            "segments:\n  - name: main\n    type: code\n    start: 0x20\n    vram: 0x80002000\n"
            "    subsegments:\n      - [0x20, c, alpha]\n      - [0x30, asm, beta]\n"
            '      - [0x40, rodata, "rodata/alpha/80002020"]\n'
            '      - [0x44, rodata, "rodata/shared/80002024"]\n'
            '      - [0x48, rodata, "rodata/alpha/80002028"]\n  - [0x4C]\n'
        )
        rows = pool_rows(yaml)
        self.assertEqual([row["owner"] for row in rows], ["alpha", None, "alpha"])
        ranges = unit_ranges(yaml)
        self.assertEqual(
            [(r["start"], r["end"]) for r in ranges["alpha"]["rodata_slices"]], [(0x40, 0x44), (0x48, 0x4C)]
        )

    def test_duplicate_literal_references_transfer_to_disjoint_sections(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            obj = Object(
                assemble(
                    root,
                    "alpha",
                    ".set noreorder\n.text\n.globl alpha\nalpha:\n"
                    "lui $at,%hi(literal)\nlwc1 $f0,%lo(literal)($at)\n"
                    "lui $at,%hi(literal)\nlwc1 $f2,%lo(literal)($at)\njr $ra\nnop\n"
                    ".section .rdata\nliteral: .word 0x3f800000\n",
                )
            )
            target = [0x3C018000, 0xC4203000, 0x3C018000, 0xC4225000, 0x03E00008, 0]
            image = bytearray(0x84)
            image[0x20:0x38] = struct.pack(">6I", *target)
            image[0x40:0x44] = image[0x80:0x84] = bytes.fromhex("3f800000")
            slices = [
                dict(start=0x40, end=0x44, address=0x80003000, path="rodata/alpha/80003000"),
                dict(start=0x80, end=0x84, address=0x80005000, path="rodata/alpha/80005000"),
            ]
            names = transfer_private(obj, dict(start=0x20, end=0x38, address=0x80002000), bytes(image), slices)
            rebuilt = Object(obj.path)
            self.assertEqual(names, [".unbake_pool_80003000", ".unbake_pool_80005000"])
            for name in names:
                index = rebuilt.section(name)
                assert index is not None
                self.assertEqual(rebuilt.content(index), bytes.fromhex("3f800000"))
            text = rebuilt.section(".text")
            assert text is not None
            self.assertEqual(
                [s["section"] for _, _, s in rebuilt.relocations(text)],
                [rebuilt.section(names[0])] * 2 + [rebuilt.section(names[1])] * 2,
            )
            script = "\n".join("obj/asm/data/" + row["path"] + ".rodata.o(.rodata)" for row in slices)
            linked = transfer_selectors(script, "obj/src/alpha.o", slices, names)
            self.assertNotIn("obj/asm", linked)
            self.assertEqual(linked.count("obj/src/alpha.o"), 2)
            self.assertEqual(
                transfer_private(rebuilt, dict(start=0x20, end=0x38, address=0x80002000), bytes(image), slices), names
            )

    def test_nonzero_private_tail_needs_compiler_material(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            obj = Object(
                assemble(
                    Path(directory),
                    "alpha",
                    ".set noreorder\n.text\n"
                    "lui $at,%hi(literal)\nlwc1 $f0,%lo(literal)($at)\njr $ra\nnop\n"
                    ".section .rdata\nliteral: .word 0x3f800000\n",
                )
            )
            image = bytearray(0x48)
            image[0x20:0x30] = struct.pack(">4I", 0x3C018000, 0xC4203000, 0x03E00008, 0)
            image[0x40:0x48] = bytes.fromhex("3f80000012345678")
            before = obj.path.read_bytes()
            with self.assertRaisesRegex(ValueError, "unaccounted private"):
                transfer_private(
                    obj,
                    dict(start=0x20, end=0x30, address=0x80002000),
                    bytes(image),
                    [dict(start=0x40, end=0x48, address=0x80003000)],
                )
            self.assertEqual(obj.path.read_bytes(), before)
