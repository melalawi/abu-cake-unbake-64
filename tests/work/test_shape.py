"""work.shape: shared rules judged against each compiler family's Shape (the adapter built from its cflags)."""

import dataclasses
import unittest

from unbake.compilers.families.gcc import Gcc
from unbake.compilers.families.ido import Ido
from unbake.compilers.families.mips import emitters
from unbake.config import Held
from unbake.work import shape

# The two adapters as BattleTanx and RageWars configure them.
SHAPES = {
    "gcc-2.8.1-sn64": Gcc().shape("gcc-2.8.1-sn64", ("-G0", "-mips3", "-O2", "-mgp32", "-mfp64")),
    "ido-7.1": Ido().shape("ido-7.1", ("-c", "-G0", "-non_shared", "-mips2", "-O2")),
}
EMITTED = emitters(SHAPES.values())
EPILOGUE = "8fbf0014 27bd0018 03e00008 00000000"
FRAMED = "27bdffe8 afbf0014 0c000000 00000000 " + EPILOGUE
OPCODE64 = "64840001 03e00008 00841021"
# (words, VRAM start) -> (route, evidence fragment), for every adapter.
SHARED = {
    "BT func_8008EC78_us filler": (("8ae3e172 aea18b2f " + FRAMED, 0x8008EC78), ("boundary", "8 bytes of alignment")),
    "BT func_8010CB5C_us filler": (("a6a5def8 " + FRAMED, 0x8010CB5C), ("boundary", "4 bytes of alignment")),
    "RW de stale frame": (("27bdfff0 3c01800d 03e00008 00000000", 0x8023CFC8), ("boundary", "8 bytes")),
    "RW eu-x call": (("0c09c7e5 02203021 03e00008 00000000", 0x8023D008), ("boundary", "8 bytes")),
    "RW us 64-bit words": (("65520000 5f000000 03e00008 00000000", 0x802AD1C8), ("boundary", "8 bytes")),
    "RW eu two stores are valid C": (("ac800028 ac80002c 03e00008 00000000", 0x8023CFD8), ("drafter", "complete")),
    "RW tail reads $f0": (("46010002 46001081 03e00008 e4820008", 0x8027206C), ("dead", "reads $f0")),
    "release without frame": ((EPILOGUE, 0x80001000), ("dead", "stack frame")),
    "call without saving ra": (("0c000000 00000000 03e00008 00000000", 0x80001000), ("dead", "saving ra")),
    "framed call": ((FRAMED, 0x80001000), ("drafter", "complete")),
    "long body reading s1": (("00111021 " * 16 + "03e00008 00000000", 0x80001000), ("drafter", "complete")),
}
# The ISA rule is the one quirk the two adapters disagree on today.
PER_ADAPTER = {
    "gcc-2.8.1-sn64": ((OPCODE64, 0x80001000), ("drafter", "complete")),
    "ido-7.1": ((OPCODE64, 0x80001000), ("dead", "outside -mips2")),
}


def words(text: str) -> bytes:
    return bytes.fromhex(text.replace(" ", ""))


def owned(data: bytes, address: int, target):
    from unbake.layout import boundary
    return boundary.evidence({i * 4: word for i, word in enumerate(shape.words_of(data))},
                             0, len(data), address, {"fixture-placement"}, set(), target)


class ShapeRuleTests(unittest.TestCase):
    def test_closed_multiple_returns_are_drafted_but_bundled_intervals_are_refused(self) -> None:
        multiple = words("10800003 00000000 03e00008 00801025 03e00008 00001025")
        bundled = words("03e00008 00000000 27bdfff0 afbf000c 8fbf000c 03e00008 27bd0010")
        for target in SHAPES.values():
            self.assertEqual(shape.classify(multiple, 0x80001000, target, EMITTED,
                             owned(multiple, 0x80001000, target))[0], "drafter")
            route, cause = shape.classify(bundled, 0x80001000, target, EMITTED, owned(bundled, 0x80001000, target))
            self.assertEqual(route, "boundary")
            self.assertIn("unowned-code-or-data-island", cause)

    def test_native_capability_rules_remain_after_boundary_proof(self) -> None:
        data = words("bd100000 03e00008 00000000")
        target = SHAPES["ido-7.1"]
        self.assertEqual(shape.classify(data, 0x80001000, target, EMITTED,
                         owned(data, 0x80001000, target))[0], "original")

    def test_adapter_fields_come_from_the_effective_flags(self) -> None:
        for family, flags, level in [(Gcc(), ("-mips2", "-mips3", "-mgp32"), 3),
                                      (Ido(), ("-G0", "-mips2", "-O2"), 2)]:
            target = family.shape("fixture", flags)
            self.assertEqual((target.isa_level, target.object_alignment), (level, 16))
        with self.assertRaises(Held):
            Ido().shape("ido-7.1", ("-O2",))
        with self.assertRaisesRegex(Held, "unsupported -mgp"):
            Gcc().shape("gcc-2.8.1-sn64", ("-mips3", "-mgp64"))
