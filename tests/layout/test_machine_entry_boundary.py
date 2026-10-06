"""A machine initialization entry is not an ordinary caller-owned C frame."""

import unittest
from pathlib import Path

from tests.work.test_shape import SHAPES
from unbake.layout import boundary


class MachineEntryBoundaryTests(unittest.TestCase):
    def test_native_stack_initialization_remains_explicitly_unproved_in_both_loading_contexts(self):
        data = (Path(__file__).parent / "fixtures/machine_entry.text").read_bytes()
        self.assertEqual(len(data), 256)
        words = {at: int.from_bytes(data[at : at + 4], "big") for at in range(0, len(data), 4)}
        self.assertEqual([words[0], words[4]], [0x3C1D803F, 0x37BDFFC0])
        for address in (0x80000400, 0x80200400):
            with self.subTest(address=address):
                proof = boundary.evidence(
                    words, 0, len(data), address, {"native-entry-nomination"}, set(), SHAPES["gcc-2.8.1-sn64"]
                )
                self.assertFalse(proof.proven)
                self.assertIn(f"unresolved-stack-write:0x{address:X}", proof.unproven)
