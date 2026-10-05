"""inventory.classify: a body with one complete return that no compiler emits is dead, never a draft candidate."""

import unittest

from unbake.work import inventory

CASES = {
    # RW dead gaps after an epilogue (live RW d46ba78); each row is one version's 16 bytes.
    "de frame without release": ("27bdfff0 3c01800d 03e00008 00000000", "dead", "stack frame"),
    "eu-x call without saving ra": ("0c09c7e5 02203021 03e00008 00000000", "dead", "call without saving ra"),
    "us-rev1 call without saving ra": ("0c09c7e9 02203021 03e00008 00000000", "dead", "call without saving ra"),
    # Valid C by shape (two stores through arg0); only reference evidence could call it dead.
    "eu two stores": ("ac800028 ac80002c 03e00008 00000000", "drafter", "one complete return"),
    "release without frame": ("8fbf0014 27bd0018 03e00008 00000000", "dead", "stack frame"),
    "reads s1 never saved": ("00111021 00000000 03e00008 00000000", "dead", "callee-saved $17"),
    "framed call": (
        "27bdffe8 afbf0014 0c000000 00000000 8fbf0014 27bd0018 03e00008 00000000",
        "drafter",
        "one complete return",
    ),
    "saved s0": (
        "27bdffe8 afb00010 00808021 02001021 8fb00010 27bd0018 03e00008 00000000",
        "drafter",
        "one complete return",
    ),
}


class NotCTests(unittest.TestCase):
    def test_routes(self) -> None:
        for name, (words, route, evidence) in CASES.items():
            with self.subTest(name):
                got = inventory.classify(bytes.fromhex(words.replace(" ", "")))
                self.assertEqual(got[0], route)
                self.assertIn(evidence, got[1])
