"""Compiler string literals survive the SN64 assembly normaliser byte-exactly."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tests.support import tool
from unbake.project_tools.elf import Object
from unbake.project_tools.sn64_gnu_as import assemble
from unbake.project_tools.sn64_gnu_as import directives as normalize


class StringLiteralTests(unittest.TestCase):
    def test_string_literals_lower_to_exact_bytes(self) -> None:
        cases = (
            ('"%s"', b"%s\0"),
            ('""', b"\0"),
            (r'"\0007\001\177\200\377"', bytes.fromhex("00 37 01 7f 80 ff 00")),
            (r'"\a\b\f\n\r\t\v\"\\"', bytes.fromhex("07 08 0c 0a 0d 09 0b 22 5c 00")),
            ('".rodata,#", "tail" # comment', b".rodata,#\0tail\0"),
            ('"0123456789abcdefghijkl"', b"0123456789abcdefghijkl\0"),
        )
        for operand, expected in cases:
            with self.subTest(operand=operand):
                result = normalize(f".rodata\n.align 2\nLC0: .string {operand}\n.text\n")
                lines = result.decode().splitlines()
                self.assertEqual(lines[:2], [".rdata", ".align 2"])
                self.assertTrue(lines[2].startswith("LC0: .byte "))
                self.assertEqual(lines[-1], ".text")
                actual = bytes(int(value) for line in lines[2:-1] for value in line.split(".byte ")[1].split(","))
                self.assertEqual(actual, expected)
        for operand in ("", '"unterminated', r'"\q"', '"ok",', '"ok" junk'):
            with self.subTest(invalid_operand=operand), self.assertRaisesRegex(ValueError, r"\.string:"):
                normalize(f".string {operand}\n")


class AssemblyRegressionTests(unittest.TestCase):
    def test_real_assembly_rule_families(self) -> None:
        fixtures = Path(__file__).parents[1] / "fixture/sn64"
        assembler = Path(tool("mips-linux-gnu-as"))
        with tempfile.TemporaryDirectory() as temporary:
            for source in sorted(fixtures.glob("*.s")):
                with self.subTest(family=source.stem):
                    output = Path(temporary) / (source.stem + ".o")
                    assemble(source.read_text(), output, assembler, ["-mips3"])
                    actual = Object(output)
                    if source.stem == "common-bss":
                        symbols = {symbol["name"]: symbol for table in actual.symbols.values() for symbol in table}
                        self.assertEqual(
                            {name: symbols[name]["value"] for name in ("local_words", "shared_small", "shared_large")},
                            {"local_words": 0, "shared_small": 12, "shared_large": 16},
                        )
                    expected = json.loads(source.with_suffix(".json").read_text())
                    for name, content in expected.items():
                        section = actual.section(name)
                        payload = actual.content(section) if section is not None else b""
                        self.assertEqual(payload, bytes.fromhex(content), name)
