"""Measured text directives become data without losing real instructions."""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from unbake.layout import split, split_partition


class TextTypingTests(unittest.TestCase):
    def classify(self, bodies: list[str]) -> str:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            assembly = root / "asm/us"
            assembly.mkdir(parents=True)
            text = ".section .text\nglabel handler\n"
            for index, body in enumerate(bodies):
                text += f"/* {16 + index * 4:X} {0x80000010 + index * 4:08X} 00000000 */ {body}\n"
            (assembly / "handler.s").write_text(text)
            layout = root / "split.yaml"
            layout.write_text(
                "segments:\n  - name: main\n    type: code\n    start: 0x10\n"
                "    vram: 0x80000010\n    subsegments:\n      - [0x10, hasm, handler]\n"
                f"  - [0x{16 + len(bodies) * 4:X}]\n"
            )
            measured = split.extracted_text(SimpleNamespace(asm=root / "asm"), "us")
            return split_partition.type_text(layout, measured)

    def test_raw_word_run_is_data(self) -> None:
        text = self.classify([".word 0x12345678", ".word 0xFFFFFFFF /* invalid instruction */"])
        self.assertIn("[0x10, data, handler]", text)
        self.assertEqual(text.count(", data,"), 1)

    def test_string_is_data(self) -> None:
        self.assertIn("[0x10, data, handler]", self.classify(['.ascii "test"']))

    def test_mixed_interval_keeps_exact_instruction_boundaries(self) -> None:
        text = self.classify(["jr $ra", "nop", ".word 0xFFFFFFFF", "eret"])
        self.assertIn("[0x10, hasm, handler]", text)
        self.assertIn("[0x18, data, handler_data_18]", text)
        self.assertIn("[0x1C, hasm, handler_hasm_1C]", text)

    def test_handwritten_exception_handler_stays_code(self) -> None:
        text = self.classify(["mfc0 $k0, $30 /* handwritten instruction */", "jr $k0", "nop", "eret"])
        self.assertIn("[0x10, hasm, handler]", text)
        self.assertNotIn(", data,", text)
