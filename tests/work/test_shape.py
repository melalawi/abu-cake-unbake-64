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
OPCODE64 = "64840001 03e00008 00000000"
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


class ShapeRuleTests(unittest.TestCase):
    def test_routes_for_every_adapter(self) -> None:
        for ident, target in SHAPES.items():
            for name, ((text, address), (route, evidence)) in {**SHARED, "isa": PER_ADAPTER[ident]}.items():
                with self.subTest(ident=ident, case=name):
                    got = shape.classify(words(text), address, target, EMITTED)
                    self.assertEqual(got[0], route, got[1])
                    self.assertIn(evidence, got[1])

    def test_an_adapter_switches_a_rule_off(self) -> None:
        # rule left out -> the case that rule decided
        cases = {
            "filler": ("BT func_8010CB5C_us filler", "drafter"),
            "frame": ("release without frame", "drafter"),
            "call_ra": ("call without saving ra", "drafter"),
            "entry_registers": ("RW tail reads $f0", "drafter"),
        }
        base = SHAPES["ido-7.1"]
        for rule, (case, route) in cases.items():
            with self.subTest(rule):
                target = dataclasses.replace(base, rules=base.rules - {rule})
                (text, address), _ = SHARED[case]
                self.assertEqual(shape.classify(words(text), address, target, EMITTED)[0], route)

    def test_adapter_fields_come_from_the_compiler_flags(self) -> None:
        cases = [
            (Gcc(), ("-G0", "-mips3", "-mgp32"), 3),
            (Ido(), ("-G0", "-mips2", "-O2"), 2),
            (Gcc(), ("-mips2", "-mips3"), 3),
        ]
        for family, flags, level in cases:
            with self.subTest(flags):
                target = family.shape("c", flags)
                self.assertEqual((target.isa_level, target.object_alignment, target.fragment_bytes), (level, 16, 64))
        with self.assertRaises(Held) as refused:
            Ido().shape("ido-7.1", ("-O2",))
        self.assertIn("compilers.ido-7.1.cflags: required -mipsN", str(refused.exception))

    def test_a_frameless_row_after_a_body_with_no_return_is_its_tail(self) -> None:
        owner, cut = words("c4a20004 c4c00008 46001082"), words("46010002 46001081 03e00008 e4820008")
        target = SHAPES["gcc-2.8.1-sn64"]
        self.assertTrue(shape.tail(owner, 0x80272018, cut, target, EMITTED))
        self.assertFalse(shape.tail(words(EPILOGUE), 0x80272018, cut, target, EMITTED))
        self.assertFalse(shape.tail(owner, 0x80272018, words(FRAMED), target, EMITTED))
        self.assertFalse(shape.tail(owner, 0x80272018, cut, dataclasses.replace(target, rules=frozenset()), EMITTED))
