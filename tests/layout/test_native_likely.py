"""Actual IDO 5.3/7.1 O2/O3 delay-slot copies remain part of their native function."""

import unittest

from tests.layout.test_boundary_proof import prove
from tests.work.test_shape import SHAPES

# native_matrix.py probe.c, IDO 5.3 O2 .text symbols branch and framed_multi.
BRANCH = "00a4082a 50200006 8cd80000 8cce0000 01c47821 10000004 accf0000 8cd80000 0305c823 acd90000 03e00008 8cc20000"
FRAMED = (
    "27bdffe8 afbf0014 0c000000 00000000 04410003 00401825 10000008 2462ffff 54400006 24620005 "
    "0c000000 24040007 10000003 8fbf0014 24620005 8fbf0014 27bd0018 03e00008 00000000"
)


class NativeLikelyTests(unittest.TestCase):
    def test_emitted_delay_copies_are_owned_without_executing_them_again(self):
        for text in (BRANCH, FRAMED):
            with self.subTest(text=text):
                verdict = prove(text, target=SHAPES["ido-7.1"])
                self.assertTrue(verdict.proven, verdict.unproven)

    def test_unrelated_dead_word_and_independent_entry_remain_unproved(self):
        changed = BRANCH.split()
        changed[7] = "8cd80004"
        for text, known in ((" ".join(changed), ()), (BRANCH, (28,)), (BRANCH.replace("50200006", "10200006"), ())):
            with self.subTest(text=text, known=known):
                self.assertFalse(prove(text, known=known, target=SHAPES["ido-7.1"]).proven)
