"""Public classification repairs measured boundaries and fragment types."""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import cast

from unbake.layout import split_partition
from unbake.layout.split_audit import audit
from unbake.project.config import Project


class AuditTests(unittest.TestCase):
    def check(self, body: str, symbols: str = "") -> tuple[str, list[str]]:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            assembly = root / "asm/us/nested"
            assembly.mkdir(parents=True)
            (assembly / "entry.s").write_text(".section .text\nglabel entry\n" + body)
            layout = root / "split.yaml"
            layout.write_text(
                "segments:\n  - name: main\n    type: code\n    start: 0x10\n"
                "    vram: 0x80000010\n    subsegments:\n      - [0x10, asm, nested/entry]\n  - [0x20]\n"
            )
            syms = root / "symbols.txt"
            syms.write_text(symbols)
            project = cast(
                Project,
                SimpleNamespace(asm=root / "asm", version=lambda _: SimpleNamespace(split=layout, symbols=syms)),
            )
            findings, edits = audit(project, "us")
            self.assertEqual(split_partition.classify(project, "us"), edits)
            return edits[0].after if edits else layout.read_text(), [finding.kind for finding in findings]

    def test_hidden_entry_is_cut_without_losing_directory(self) -> None:
        for label in ("glabel inner\n", ""):
            with self.subTest(label=label):
                text, kinds = self.check(
                    "/* 10 80000010 03E00008 */ jr $ra\n/* 14 80000014 00000000 */ nop\n"
                    + label
                    + "/* 18 80000018 03E00008 */ jr $ra\n/* 1C 8000001C 00000000 */ nop\n",
                    "inner = 0x80000018;\n",
                )
                self.assertIn("[0x18, asm, nested/inner]", text)
                self.assertEqual(kinds, ["hidden"])

    def test_local_jump_stub_becomes_handwritten_but_tail_thunk_stays_function(self) -> None:
        for target, expected in ((".L80000040", "hasm"), ("other", "asm")):
            with self.subTest(target=target):
                text, kinds = self.check(
                    f"/* 10 80000010 08000010 */ j {target}\n"
                    "/* 14 80000014 00000000 */ nop\n"
                    "/* 18 80000018 00000000 */ nop\n/* 1C 8000001C 00000000 */ nop\n"
                )
                self.assertIn(f", {expected}, nested/entry]", text)
                self.assertEqual(kinds, ["fragment"] if expected == "hasm" else [])

    def test_function_row_with_data_is_typed_at_exact_offsets(self) -> None:
        text, kinds = self.check(
            "/* 10 80000010 03E00008 */ jr $ra\n/* 14 80000014 00000000 */ nop\n"
            "/* 18 80000018 FFFFFFFF */ .word 0xFFFFFFFF\n"
            "/* 1C 8000001C 12345678 */ .word 0x12345678\n"
        )
        self.assertIn("[0x10, asm, nested/entry]", text)
        self.assertIn("[0x18, data, nested/entry_data_18]", text)
        self.assertEqual(kinds, ["data"])
