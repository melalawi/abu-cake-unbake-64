"""inventory.classify: leading alignment filler is a boundary, and only a short unbalanced body is dead."""

import unittest

from unbake.work import inventory

EPILOGUE = "8fbf0014 27bd0018 03e00008 00000000"
FRAMED = "27bdffe8 afbf0014 0c000000 00000000 " + EPILOGUE
CASES = {
    # (words, VRAM start, ISA level) -> (route, evidence fragment)
    # BT us: filler attached before a 16-aligned prologue.
    "BT func_8008EC78_us filler": (
        ("8ae3e172 aea18b2f " + FRAMED, 0x8008EC78, 3),
        ("boundary", "8 bytes of alignment"),
    ),
    "BT func_8010CB5C_us filler": (("a6a5def8 " + FRAMED, 0x8010CB5C, 3), ("boundary", "4 bytes of alignment")),
    # RW 24-byte gaps: filler, then an aligned empty function.
    "RW de stale frame": (("27bdfff0 3c01800d 03e00008 00000000", 0x8023CFC8, 3), ("boundary", "8 bytes")),
    "RW eu-x call": (("0c09c7e5 02203021 03e00008 00000000", 0x8023D008, 3), ("boundary", "8 bytes")),
    "RW us 64-bit words": (("65520000 5f000000 03e00008 00000000", 0x802AD1C8, 3), ("boundary", "8 bytes")),
    "RW eu two stores are valid C": (("ac800028 ac80002c 03e00008 00000000", 0x8023CFD8, 3), ("drafter", "complete")),
    # Short fragments no compiler emits.
    "RW tail reads $f0": (("46010002 46001081 03e00008 e4820008", 0x8027206C, 3), ("dead", "reads $f0")),
    "release without frame": ((EPILOGUE, 0x80001000, 3), ("dead", "stack frame")),
    "call without saving ra": (("0c000000 00000000 03e00008 00000000", 0x80001000, 3), ("dead", "saving ra")),
    "64-bit opcode under -mips2": (("64840001 03e00008 00000000", 0x80001000, 2), ("dead", "outside -mips2")),
    # Real functions are never judged dead: a balanced frame, or a body longer than a fragment.
    "framed call": ((FRAMED, 0x80001000, 3), ("drafter", "complete")),
    "long body reading s1": (("00111021 " * 16 + "03e00008 00000000", 0x80001000, 3), ("drafter", "complete")),
}


class NotCTests(unittest.TestCase):
    def test_routes(self) -> None:
        for name, ((words, address, level), (route, evidence)) in CASES.items():
            with self.subTest(name):
                got = inventory.classify(bytes.fromhex(words.replace(" ", "")), address, level)
                self.assertEqual(got[0], route, got[1])
                self.assertIn(evidence, got[1])

    def test_isa_level_comes_from_the_compiler_flags(self) -> None:
        for flags, level in [(("-G0", "-mips2", "-O2"), 2), (("-G0", "-mips3", "-mgp32"), 3), (("-O2",), 0)]:
            self.assertEqual(inventory.isa(flags), level)

    def test_a_frameless_row_after_a_body_with_no_return_is_its_tail(self) -> None:
        owner = bytes.fromhex("c4a20004 c4c00008 46001082".replace(" ", ""))
        tail = bytes.fromhex("46010002 46001081 03e00008 e4820008".replace(" ", ""))
        self.assertTrue(inventory.tail(owner, 0x80272018, tail))
        self.assertFalse(inventory.tail(bytes.fromhex(EPILOGUE.replace(" ", "")), 0x80272018, tail))
        self.assertFalse(inventory.tail(owner, 0x80272018, bytes.fromhex(FRAMED.replace(" ", ""))))
