"""Compiler literals can occupy a shared pool without source alignment tricks."""

import os
import struct
import tempfile
import unittest
from pathlib import Path

from tests.decomp.support import assemble
from unbake.project_tools.elf import Object
from unbake.project_tools.layout import resident
from unbake.project_tools.literal_layout import arrange
from unbake.project_tools.rodata import placement, relocated


class LiteralLayoutTests(unittest.TestCase):
    def test_anonymous_same_value_symbols_keep_independent_hi_lo_pairs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            obj = Object(
                assemble(
                    Path(temporary).resolve(),
                    "anonymous",
                    ".set noreorder\n.text\n"
                    "lui $t0,%hi(first)\nlui $t1,%hi(second)\n"
                    "lwc1 $f0,%lo(first)($t0)\nlwc1 $f2,%lo(second)($t1)\njr $ra\nnop\n"
                    ".section .rdata\n.globl first,second\nfirst:\nsecond: .word 0x3f800000\n",
                )
            )
            for index, symbols in obj.symbols.items():
                data = bytearray(obj.content(index))
                for number, symbol in enumerate(symbols):
                    if symbol["name"] in ("first", "second"):
                        struct.pack_into(">I", data, number * 16, 0)
                from unbake.project_tools.literal_layout import replace

                replace(obj, index, data)
            obj.path.write_bytes(obj.data)
            obj = Object(obj.path)
            raw = bytes.fromhex("3f8000003f800000")

            def read(address: int, size: int) -> bytes:
                return raw[address - 0x80003000 : address - 0x80003000 + size]

            target = {0: 0x3C088000, 4: 0x3C098000, 8: 0xC5003000, 12: 0xC5223004}
            shared = obj.path.with_suffix(".shared")
            os.link(obj.path, shared)
            original = shared.read_bytes()
            self.assertEqual(arrange(obj, ".rdata", target, 0x80002000, read), 0x80003000)
            self.assertEqual(relocated(Object(obj.path), ".rdata", 0x80002000), raw)
            self.assertEqual(shared.read_bytes(), original)
            self.assertNotEqual(shared.stat().st_ino, obj.path.stat().st_ino)

    def anchored(self, values: list[tuple[int, bytes]], *, external: bool = True) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            asm = ".set noreorder\n.text\n.globl alpha\nalpha:\n"
            if external:
                asm += "lui $at,%hi(external+4)\nlwc1 $f0,%lo(external+4)($at)\n"
            asm += "jr $ra\nnop\n.section .rdata\n"
            for address, data in values:
                name = f"unbake_rodata_{address:08X}_{len(data):X}"
                asm += f".align 3\n.globl {name}\n{name}:\n.byte " + ",".join(str(v) for v in data) + "\n"
            obj = Object(assemble(root, "storage", asm))
            text = obj.section(".text")
            assert text is not None
            before = obj.content(text)
            relocations = obj.relocations(text)
            base = min(a for a, _ in values)
            end = max(a + len(data) for a, data in values)
            raw = bytearray(end - base)
            for address, data in values:
                raw[address - base : address - base + len(data)] = data

            def read(address: int, size: int) -> bytes:
                return bytes(raw[address - base : address - base + size])

            for _ in range(2):
                self.assertEqual(arrange(obj, ".rdata", {}, 0x80002000, read, emit_resident=True), base)
                rebuilt = Object(obj.path)
                self.assertEqual(relocated(rebuilt, ".rdata", 0x80002000), bytes(raw))
                self.assertEqual(rebuilt.content(text), before)
                self.assertEqual(rebuilt.relocations(text), relocations)
                for symbols in rebuilt.symbols.values():
                    for symbol in symbols:
                        if symbol["name"].startswith("unbake_rodata_"):
                            address = int(symbol["name"].split("_")[2], 16)
                            self.assertEqual(symbol["value"], address - base)

    def test_pool_offset_storage_keeps_external_addends(self) -> None:
        self.anchored([(0x80003004, struct.pack(">f", 2.0))])

    def test_incomplete_volatile_pool_emits_all_sixteen_bytes(self) -> None:
        self.anchored([(0x80003000 + i * 4, struct.pack(">f", float(i))) for i in range(4)])

    def test_missing_leading_pool_is_explicitly_emitted(self) -> None:
        self.anchored([(0x80003000, struct.pack(">f", 1.0)), (0x80003004, struct.pack(">f", 2.0))])

    def test_extra_nonpadding_has_an_explicit_storage_identity(self) -> None:
        self.anchored([(0x80003000, bytes.fromhex("123456789abcdef0")), (0x80003008, b"more")])

    def test_address_aggregate_array_storage_is_not_a_string(self) -> None:
        self.anchored([(0x80003000, bytes.fromhex("01020304000000000000000040c00000"))])

    def test_nonpool_instruction_changes_are_avoided(self) -> None:
        self.anchored([(0x80003000, struct.pack(">f", 0.0))])

    def test_byte_array_pool_has_its_exact_unrounded_extent(self) -> None:
        self.anchored([(0x80003001, b"abc\0")])

    def test_existing_short_string_pool_has_explicit_nonzero_tail(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            obj = Object(
                assemble(
                    root,
                    "short_string",
                    ".set noreorder\n.text\n"
                    "lui $at,%hi(string)\naddiu $a0,$at,%lo(string)\njr $ra\nnop\n"
                    '.section .rdata\nstring: .asciz "fourteen chars"\n'
                    ".globl unbake_rodata_8000300F_1\nunbake_rodata_8000300F_1: .byte 255\n",
                )
            )
            raw = b"fourteen chars\0\xff"
            self.assertEqual(len(raw), 16)

            def read(address: int, size: int) -> bytes:
                return raw[address - 0x80003000 : address - 0x80003000 + size]

            self.assertEqual(
                arrange(obj, ".rdata", {0: 0x3C018000, 4: 0x24243000}, 0x80002000, read, emit_resident=True), 0x80003000
            )
            self.assertEqual(relocated(Object(obj.path), ".rdata", 0x80002000), raw)

    def test_shared_duplicates_and_misaligned_table_prove_each_word(self) -> None:
        for biased in (False, True):
            with self.subTest(biased=biased), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary).resolve()
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
