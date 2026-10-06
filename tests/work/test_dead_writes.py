"""Instruction semantics are independent of trap code fields and compiler optimization."""

import unittest

from unbake.work.shape import _registers


class RegisterSemantics(unittest.TestCase):
    def test_syscall_and_break_code_fields_are_not_register_writes(self):
        for word in (0x0007000D, 0x0006000D, 0x03FFFFCC, 0x0000000C):
            self.assertEqual(_registers(word), (set(), set(), set(), set()))

    def test_adjacent_special_instructions_keep_their_operands(self):
        self.assertEqual(_registers(0x0085001A)[:2], ({4, 5}, set()))
        self.assertEqual(_registers(0x00001012)[:2], (set(), {2}))
        self.assertEqual(_registers(0x00851021)[:2], ({4, 5}, {2}))
