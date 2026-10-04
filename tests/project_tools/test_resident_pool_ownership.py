"""Resident C storage and structural assembly pools retain separate providers."""

import argparse
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.decomp.support import assemble
from unbake.objects.elf import Object
from unbake.project_tools.layout import place_object, resident_slices
from unbake.project_tools.link_inputs import Objects, Selectors, Spans
from unbake.objects.rodata import relocated


class ResidentPoolTests(unittest.TestCase):
    def test_mixed_anchors_and_resident_only_baseline(self):
        for kind in ("resident", "mixed", "crossing", "disagree"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                target = root / "obj/src"
                target.mkdir(parents=True)
                first = "unbake_rodata_80003000_8" if kind == "crossing" else "unbake_rodata_80003000_4"
                extra = (
                    ""
                    if kind == "crossing"
                    else ".globl unbake_rodata_80003004_4\nunbake_rodata_80003004_4: .word 0x87654321\n"
                )
                objpath = assemble(
                    target,
                    "alpha",
                    ".text\njr $ra\nnop\n.section .rdata\n.globl "
                    + first
                    + "\n"
                    + first
                    + ": .word 0x12345678\n"
                    + (".word 0x87654321\n" if kind == "crossing" else extra),
                )
                original = objpath.read_bytes()
                image = bytearray(0x48)
                image[0x40:0x48] = bytes.fromhex("1234567887654321")
                if kind == "disagree":
                    image[0x40] = 0
                mapping = dict(address=0x80003000, start=0x40, end=0x48, table_entry_bias=0)
                pool = dict(address=0x80003004, start=0x44, end=0x48, path="rodata/shared/80003004")
                pools = (
                    [pool]
                    if kind in ("mixed", "crossing")
                    else [dict(address=0x80004000, start=0x80, end=0x84, path="rodata/other")]
                )
                providers, fragments = [], []
                args = argparse.Namespace(build=root)
                inputs = (
                    args,
                    "obj/src/alpha.o",
                    "obj/src/alpha.o(.text)",
                    {"alpha": dict(start=0, end=8, address=0x80002000)},
                    bytes(image),
                    [mapping],
                    fragments,
                    False,
                )
                inventory = resident_slices(pools, [mapping])
                optimized = dict(inventory=inventory, lookup=Spans(inventory), selectors=Selectors(inputs[2]))
                if kind == "disagree":
                    with self.assertRaisesRegex(ValueError, "explicit storage disagrees"):
                        place_object(*inputs, pools=pools, providers=providers, **optimized)
                    self.assertEqual(objpath.read_bytes(), original)
                    continue
                with Objects(root / "metadata") as load:
                    place_object(*inputs, pools=pools, providers=providers, load=load, **optimized)
                self.assertEqual([p.address for p in providers], [0x80003004] if kind in ("mixed", "crossing") else [])
                self.assertTrue(fragments)
                obj = Object(objpath)
                material = b"".join(relocated(obj, n, 0x80002000) for n in obj.names if n.startswith(".unbake_pool_"))
                self.assertEqual(material, image[0x40:0x48])
                self.assertEqual(obj.content(obj.section(".rdata")), b"")

    def test_prepared_inventory_skips_per_object_partition(self):
        for empty in (True, False):
            with self.subTest(empty=empty), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                target = root / "obj/src"
                target.mkdir(parents=True)
                text = ".text\njr $ra\nnop\n"
                if not empty:
                    text += (
                        ".section .rdata\n.globl unbake_rodata_80003000_4\nunbake_rodata_80003000_4: .word 0x12345678\n"
                    )
                assemble(target, "alpha", text)
                row = dict(address=0x80003000, start=0x40, end=0x44, path="rodata/shared/80003000")
                image = bytes(0x40) + bytes.fromhex("12345678")
                providers = []
                with patch("unbake.project_tools.layout.resident_slices") as partition:
                    place_object(
                        argparse.Namespace(build=root),
                        "obj/src/alpha.o",
                        "obj/src/alpha.o(.text)",
                        {"alpha": dict(start=0, end=8, address=0x80002000)},
                        image,
                        [],
                        [],
                        False,
                        pools=[row],
                        providers=providers,
                        inventory=[row],
                    )
                    partition.assert_not_called()
                self.assertEqual(len(providers), 0 if empty else 1)

    def test_resident_gap_partition_table(self):
        mapping = dict(address=0x80003000, start=0x40, end=0x50, table_entry_bias=7)
        for offset, size, gaps in ((0, 4, [(4, 16)]), (4, 4, [(0, 4), (8, 16)]), (0, 16, [])):
            with self.subTest(offset=offset, size=size):
                row = dict(address=0x80003000 + offset, start=0x40 + offset, end=0x40 + offset + size)
                rows = resident_slices([row], [mapping])
                self.assertEqual(rows[0], row)
                self.assertEqual([(r["start"] - 0x40, r["end"] - 0x40) for r in rows[1:]], gaps)
                self.assertTrue(all(r["resident"] and r["table_entry_bias"] == 7 for r in rows[1:]))
