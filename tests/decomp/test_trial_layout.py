"""Shared split metadata for function trials and ROM mappings."""

import tempfile
import unittest
from pathlib import Path

from unbake.decomp import trial_layout
from unbake.project.config import Held, Version


def resident_copy() -> list[dict[str, int]]:
    return [{"address": 0x800C0000, "start": 0x40, "end": 0x50, "table_entry_bias": 0}]


class TrialLayoutTests(unittest.TestCase):
    def test_aligned_row_uses_shared_boundaries_and_rom_mapping(self) -> None:
        endings = (
            "  - [0x50]\n",
            "  - {start: 0x50, type: bin, name: tail}\n  - [0x60]\n",
            "      - [0x50, .bss, globals]\n  - [0x60]\n",
        )
        words = bytes.fromhex("24020001 03e00008 00000000 00000000")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "game.yaml"
            rom = root / "baserom.z64"
            rom.write_bytes(bytes(0x40) + words + bytes(0x10))
            version = Version("us", rom, "", path, root / "symbols.txt", ())
            for ending in endings:
                with self.subTest(ending=ending):
                    path.write_text(
                        "segments:\n  - name: code\n    type: code\n    start: 0x40\n"
                        "    vram: 0x80001000\n    subalign: 4\n    subsegments:\n"
                        "      - [0x40, c, alpha, {align: 16}]\n" + ending,
                        encoding="utf-8",
                    )
                    span = trial_layout.function_span(version, "alpha", {})
                    self.assertEqual(span, trial_layout.FunctionSpan(0x80001000, 0x40, 0x10, 4))
                    assert span is not None
                    self.assertEqual(trial_layout.target(version, span), words)
                    reader = trial_layout.rom_reader(version, list)
                    self.assertEqual(reader(0x80001000, 0x10), words)
                    with self.assertRaisesRegex(Held, "unmapped"):
                        reader(0x80001010, 4)
                    # A configured resident copy of the same ROM bytes is readable at its runtime address.
                    copied = trial_layout.rom_reader(version, resident_copy)
                    self.assertEqual(copied(0x800C0004, 4), words[4:8])
                    self.assertEqual(copied(0x80001000, 0x10), words)
                    with self.assertRaisesRegex(Held, "unmapped"):
                        copied(0x800C000C, 8)
