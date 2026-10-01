"""Compiler string literals survive the ASN64 assembly adapter byte-exactly."""

from __future__ import annotations

import unittest

from unbake.project_tools.asn64 import normalize


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
