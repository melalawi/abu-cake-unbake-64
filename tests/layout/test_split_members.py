"""split.members: a merged C row lists one function per inner `type: func` entry; other rows pass through."""

import unittest
from unittest.mock import patch

from unbake.layout import split
from unbake.layout.split import Function

MERGED = Function(
    "us", "f_a", 0x100, 0x140, 0x80001000, "f_a", "c", ("f_a", "alias_a"), (("f_a", 0), ("f_b", 0x10), ("f_c", 0x2C))
)
ASM_WITH_ENTRY = Function("us", "g", 0x140, 0x160, 0x80001040, "g", "asm", ("g",), (("g", 0), ("g_inner", 8)))
PLAIN = Function("us", "h", 0x160, 0x170, 0x80001060, "h", "c", ("h",), (("h", 0),))


class MembersTests(unittest.TestCase):
    def test_members(self) -> None:
        # rows from split.functions -> (name, start, end, address, aliases) per member
        cases = {
            "merged C row splits at every inner entry": (
                [MERGED],
                [
                    ("f_a", 0x100, 0x110, 0x80001000, ("f_a", "alias_a")),
                    ("f_b", 0x110, 0x12C, 0x80001010, ("f_b",)),
                    ("f_c", 0x12C, 0x140, 0x8000102C, ("f_c",)),
                ],
            ),
            "asm and single-function rows pass through": (
                [ASM_WITH_ENTRY, PLAIN],
                [
                    ("g", 0x140, 0x148, 0x80001040, ("g",)),
                    ("g_inner", 0x148, 0x160, 0x80001048, ("g_inner",)),
                    ("h", 0x160, 0x170, 0x80001060, ("h",)),
                ],
            ),
        }
        for name, (rows, expected) in cases.items():
            with self.subTest(name), patch.object(split, "functions", return_value=rows):
                got = split.members(object(), "us")  # type: ignore[arg-type]
                self.assertEqual([(m.name, m.start, m.end, m.address, m.aliases) for m in got], expected)
