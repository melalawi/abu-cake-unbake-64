"""Jump table labels land by instruction index, and each table stops where the next table begins."""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from unbake.config import Held
from unbake.decomp.draft_input import instruction_indexes, jump_tables
from unbake.decomp.rom import FunctionSpan

BODY = """glabel alpha
lui $a0, %hi(jtbl_80001000)
addiu $a0, $a0, %lo(jtbl_80001000)
lui $a1, %hi(jtbl_80001010)
# a comment line
nop
/* free comment */
jr $ra
nop
"""


class Reader:
    def __init__(self, words: dict[int, int]) -> None:
        self.words = words

    def span(self, address: int, size: int) -> SimpleNamespace:
        return SimpleNamespace(end=0x80002000)

    def table_entry(self, address: int) -> int:
        return self.words.get(address, 0)


def run(words: dict[int, int], body: str = BODY, size: int = 8) -> str:
    project = SimpleNamespace(version=lambda name: SimpleNamespace(symbols="symbols"))
    with (
        patch("unbake.decomp.rom.symbol_values", return_value={}),
        patch("unbake.decomp.rom.function_span", return_value=FunctionSpan(0x80000000, 0, size)),
        patch("unbake.decomp.rom.project_reader", return_value=Reader(words)),
    ):
        return jump_tables(project, "de", "alpha", body)


class JumpTableTests(unittest.TestCase):
    def test_instruction_lines_skip_labels_directives_and_comments(self) -> None:
        lines = BODY.split("\n")
        self.assertEqual(instruction_indexes(lines, "alpha"), [1, 2, 3, 5, 7, 8])

    def test_labels_land_by_instruction_index_and_each_table_stops_at_the_next(self) -> None:
        # Every word of the first table points into the function, so only the next table's address stops it.
        words = {0x80001000 + 4 * n: 0x80000004 for n in range(6)}
        words[0x80001004] = 0x80000014
        words[0x80001010] = 0x80000008
        words[0x80001014] = 0x80000010
        words[0x80001018] = 0x90000000
        result = run(words, size=24)
        lines = result.split("\n")
        self.assertEqual(lines[lines.index(".L80000004:") + 1], "addiu $a0, $a0, %lo(jtbl_80001000)")
        self.assertEqual(lines[lines.index(".L80000008:") + 1], "lui $a1, %hi(jtbl_80001010)")
        self.assertEqual(lines[lines.index(".L80000010:") + 1], "jr $ra")
        self.assertEqual(lines[lines.index(".L80000014:") + 1], "nop")
        first, second = result.split("glabel jtbl_")[1:]
        self.assertEqual(first.count(".word"), 4)
        self.assertEqual(second.count(".word"), 2)

    def test_a_target_past_the_last_instruction_refuses_by_name(self) -> None:
        body = "glabel alpha\nlui $a0, %hi(jtbl_80001000)\nnop\n"
        with self.assertRaisesRegex(Held, "0x80000010 is past the function's last instruction"):
            run({0x80001000: 0x80000010}, body, size=24)
