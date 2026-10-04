"""Shared split metadata for function trials and ROM mappings."""

import tempfile
import unittest
from pathlib import Path

from unbake.decomp import rom
from unbake.config import Held, Version


def resident_copy() -> list[dict[str, int]]:
    return [{"address": 0x800C0000, "start": 0x40, "end": 0x50, "table_entry_bias": 0}]


class RomTests(unittest.TestCase):
    def setUp(self):
        from tests.rom_fixture import install

        install(self)

    def test_native_segment_keeps_configured_table_bias_after_migration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            path, image = root / "game.yaml", root / "baserom.z64"
            path.write_text(
                "segments:\n  - name: constants\n    type: code\n    start: 0x40\n"
                "    vram: 0x800C0000\n    subsegments:\n      - [0x40, .rodata, alpha]\n  - [0x48]\n"
            )
            image.write_bytes(bytes(0x40) + bytes.fromhex("00001234 00005678"))
            version = Version("us", image, "", path, root / "symbols.txt", ())
            copy = {"address": 0x800C0000, "start": 0x40, "end": 0x48, "table_entry_bias": 0x80000000}
            reader = rom.rom_reader(version, lambda: [copy])
            self.assertEqual(reader(0x800C0000, 4), bytes.fromhex("00001234"))
            self.assertEqual(reader.table_entry(0x800C0000), 0x80001234)
            self.assertEqual(reader.table_entry(0x800C0004), 0x80005678)
            conflicting = rom.rom_reader(version, lambda: [{**copy, "start": 0x44, "end": 0x4C}])
            with self.assertRaisesRegex(Held, "table_entry: conflicting resident backing"):
                conflicting.table_entry(0x800C0000)

    def test_aligned_row_uses_shared_boundaries_and_rom_mapping(self) -> None:
        endings = (
            "  - [0x50]\n",
            "  - {start: 0x50, type: bin, name: tail}\n  - [0x60]\n",
            "      - [0x50, .bss, globals]\n  - [0x60]\n",
        )
        words = bytes.fromhex("24020001 03e00008 00000000 00000000")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            path = root / "game.yaml"
            image = root / "baserom.z64"
            image.write_bytes(bytes(0x40) + words + bytes(0x10))
            version = Version("us", image, "", path, root / "symbols.txt", ())
            for ending in endings:
                with self.subTest(ending=ending):
                    path.write_text(
                        "segments:\n  - name: code\n    type: code\n    start: 0x40\n"
                        "    vram: 0x80001000\n    subalign: 4\n    subsegments:\n"
                        "      - [0x40, c, alpha, {align: 16}]\n" + ending,
                        encoding="utf-8",
                    )
                    span = rom.function_span(version, "alpha", {})
                    self.assertEqual(span, rom.FunctionSpan(0x80001000, 0x40, 0x10))
                    assert span is not None
                    self.assertEqual(rom.target(version, span), words)
                    reader = rom.rom_reader(version, list)
                    self.assertEqual(reader(0x80001000, 0x10), words)
                    with self.assertRaisesRegex(Held, "unmapped"):
                        reader(0x80001010, 4)
                    # A configured resident copy of the same ROM bytes is readable at its runtime address.
                    copied = rom.rom_reader(version, resident_copy)
                    self.assertEqual(copied(0x800C0004, 4), words[4:8])
                    self.assertEqual(copied(0x80001000, 0x10), words)
                    with self.assertRaisesRegex(Held, "unmapped"):
                        copied(0x800C000C, 8)
