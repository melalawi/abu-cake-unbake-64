"""Frame ownership must account for every operation that can change saved control state."""

import unittest

from tests.layout.test_boundary_proof import prove


class FrameWriteTests(unittest.TestCase):
    def test_coprocessor_and_atomic_loads_cannot_silently_replace_sp_or_ra(self):
        for op in ("401d6000", "c09d0000", "c0bf0000", "e09f0000"):
            with self.subTest(op=op):
                self.assertFalse(prove(op + " 03e00008 00000000").proven)

    def test_fp_and_atomic_stores_destroy_overlapping_saved_return_addresses(self):
        for store in ("e7a0001c", "f7a00018", "e3a2001c", "f3a20018"):
            with self.subTest(store=store):
                text = "27bdffe0 afbf001c " + store + " 0c000800 00000000 8fbf001c 03e00008 27bd0020"
                self.assertFalse(prove(text).proven)

    def test_adjacent_byte_halfword_and_fp_stores_keep_the_saved_return_address(self):
        for store in ("a3a0001b", "a7a0001a", "e7a00018", "f7a00010"):
            with self.subTest(store=store):
                text = "27bdffe0 afbf001c " + store + " 0c000800 00000000 8fbf001c 03e00008 27bd0020"
                verdict = prove(text)
                self.assertTrue(verdict.proven, verdict.unproven)
