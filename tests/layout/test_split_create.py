"""Executable discovery from measured entrypoint bounds."""

import struct
import unittest

from unbake.layout.split_create import complete_executable, copied_spans, executable_end, loaded_rows


class LoadedTextTests(unittest.TestCase):
    def test_clear_loop_extends_text_but_excludes_assets(self) -> None:
        data = bytearray(0x2000)
        struct.pack_into(">I", data, 8, 0x80000400)
        words = (
            0x3C088000,
            0x25080800,
            0x3C098000,
            0x25290840,
            0x11090005,
            0,
            0x25080004,
            0x0109082B,
            0x1420FFFD,
            0xAD00FFFC,
            0x0C000110,
            0,
        )
        struct.pack_into(">" + "I" * len(words), data, 0x1000, *words)
        data[0x1040:0x1048] = bytes.fromhex("03e0000800000000")
        data[0x1100:0x1110] = bytes.fromhex("27bdfff0afbf000c03e0000827bd0010")
        data[0x1500:0x1510] = data[0x1100:0x1110]
        text = (
            "segments:\n  - name: main\n    type: code\n    start: 0x1040\n"
            "    vram: 0x80000440\n    subsegments:\n      - [0x1040, asm]\n"
            "  - type: bin\n    start: 0x1080\n  - [0x2000]\n"
        )
        output = complete_executable(text, bytes(data))
        self.assertIn("- [0x1110, data]", output)
        self.assertIn("    start: 0x1400\n", output)
        self.assertIn("    bss_end: 0x80000840\n", output)
        # Constants alone do not prove loading: the zero-store is required.
        struct.pack_into(">I", data, 0x1024, 0)
        self.assertEqual(complete_executable(text, bytes(data)), text)


class CopiedTextTests(unittest.TestCase):
    def test_rom_copy_arguments_require_cartridge_address_evidence(self) -> None:
        data = bytearray(0x4000)
        words = (0x27BDFFF0, 0x3C100000, 0x26102000, 0x3C02B000, 0x02021025, 0x02002821, 0x24061000, 0x0C000100, 0)
        struct.pack_into(">" + "I" * len(words), data, 0x1000, *words)
        self.assertIn((0x2000, 0x3000), copied_spans(bytes(data)))
        struct.pack_into(">I", data, 0x100C, 0)
        self.assertNotIn((0x2000, 0x3000), copied_spans(bytes(data)))


class LoadedMappingTests(unittest.TestCase):
    def test_calls_anchor_functions_and_exclude_other_loaded_mapping(self) -> None:
        data = bytearray(0x2000)
        bias = 0x80000000 - 0x1000
        # The resident calls a framed function in its mapping. Another loaded
        # program has an identical prologue but calls a different RAM range.
        struct.pack_into(">4I", data, 0x1000, 0x0C000040, 0, 0x03E00008, 0)
        struct.pack_into(">4I", data, 0x1100, 0x27BDFFF0, 0xAFBF000C, 0x03E00008, 0x27BD0010)
        struct.pack_into(">4I", data, 0x1800, 0x27BDFFF0, 0xAFBF000C, 0x0C100000, 0)
        end = executable_end(bytes(data), 0x1000, 0x2000, bias, seeds=[0x1100])
        self.assertEqual(end, 0x1110)
        self.assertEqual(loaded_rows(bytes(data), 0x1000, end, bias), "      - [0x1000, asm]\n      - [0x1100, asm]\n")
