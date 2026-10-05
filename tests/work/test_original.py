"""work.shape.original: the per-compiler impossible rules. The negative side is tested hardest, so the original
route can never become an escape hatch for code a compiler did emit."""

import dataclasses
import unittest

from tests.work.compiler_probe import PROBE
from unbake.compilers.families.gcc import Gcc
from unbake.compilers.families.ido import Ido
from unbake.compilers.families.mips import ORIGINAL_RULES, Shape, emitters
from unbake.work import shape

# Each probed compiler at the flags it was probed with.
ADAPTERS: dict[str, Shape] = {
    "gcc-2.7.2-kmc -mips3": Gcc().shape("gcc-2.7.2-kmc", ("-G0", "-mips3", "-mgp32", "-mfp32", "-O2")),
    "gcc-2.8.1-sn64 -mips3": Gcc().shape("gcc-2.8.1-sn64", ("-G0", "-mips3", "-O2", "-mgp32", "-mfp64")),
    "gcc-2.7.2-kmc -mips1": Gcc().shape("gcc-2.7.2-kmc", ("-G0", "-mips1", "-O2")),
    "ido-5.3 -mips2": Ido().shape("ido-5.3", ("-c", "-G0", "-non_shared", "-mips2", "-O2")),
    "ido-7.1 -mips2": Ido().shape("ido-7.1", ("-c", "-G0", "-non_shared", "-mips2", "-O2")),
}
# The compilers both games configure (KMC, SN64, IDO 5.3, IDO 7.1).
GAMES = emitters(shape for label, shape in ADAPTERS.items() if "-mips1" not in label)
IDO_ONLY = emitters([ADAPTERS["ido-7.1 -mips2"]])
JR_RA = "03e00008 00000000"


def words(text: str) -> list[int]:
    return [int(word, 16) for word in text.split()]


# Hand-written C-compiler idioms the probe did not reach, each from a real body; all must stay C.
C_IDIOMS = {
    "64-bit ops at -mips3 (dsll32, dsra32)": ("0004103c 03e00008 0002103f", GAMES),
    "lwl/lwr/swl/swr unaligned copy": ("88820000 98820003 a8a20000 b8a20003 " + JR_RA, GAMES),
    "jump table dispatch (jr t6)": ("3c0e8000 8dce0000 01c00008 00000000", GAMES),
    "leaf jr ra": (JR_RA, GAMES),
    "cop1 branches bc1t/bc1f": ("46006032 45010002 00000000 45000002 00000000 " + JR_RA, GAMES),
    "mfc1/mtc1/dmfc1/dmtc1": ("44022000 44842000 44222000 44a22000 " + JR_RA, GAMES),
}
# Words that share opcode bits with a rule but are not its instruction.
NEAR_MISSES = {
    "BT func_800AC2C8_us data (beqzl; bc0f)": "53005400 41004e00",
    "the same data words before a return": "53005400 41004e00 " + JR_RA,
    "bc0t (op 16, rs 8)": "41010003 00000000 " + JR_RA,
    "cfc0 (op 16, rs 2)": "40426000 " + JR_RA,
    "CO word with nonzero middle bits": "42100002 " + JR_RA,
    "CO function 25 (not a TLB op or eret)": "42000019 " + JR_RA,
    "cfc1 $30": "4442f000 " + JR_RA,
    "ctc1 $0": "44c40000 " + JR_RA,
    "ctc1 $31 encoding with low bits set": "44c4f801 " + JR_RA,
    "ctc1 $31 beside cvt.w.s (IDO conversion)": "44c2f800 46006124 4442f800 " + JR_RA,
    "ctc1 $31 beside trunc.w.d": "44c2f800 4620610d " + JR_RA,
    "immediate 26 (addiu v0,v0,26)": "2442001a " + JR_RA,
    "shift amount 26 (sll v0,v0,26)": "00021680 " + JR_RA,
    "mtc0 bits in a body with no return": "40846000 00000000 00000000",
    "cache bits in a body with no return": "bd010000 00000000",
    "k0 bits in a body with no return": "3c1a8000 275a0000",
}
# (words, emitted) -> rule; each fires on exactly its instruction.
POSITIVES = {
    "mfc0": ("40026000 " + JR_RA, "cop0"),
    "dmfc0": ("40226000 " + JR_RA, "cop0"),
    "mtc0": ("40846000 " + JR_RA, "cop0"),
    "dmtc0": ("40a46000 " + JR_RA, "cop0"),
    "tlbr": ("42000001 " + JR_RA, "cop0"),
    "tlbwi": ("42000002 " + JR_RA, "cop0"),
    "tlbwr": ("42000006 " + JR_RA, "cop0"),
    "tlbp": ("42000008 " + JR_RA, "cop0"),
    "eret": ("42000018 00000000", "cop0"),
    "cache": ("bd010000 " + JR_RA, "cop0"),
    "RW de __osSetFpcCsr (cfc1; ctc1 $31)": ("4442f800 44c4f800 " + JR_RA, "fcsr"),
    "cfc1 $31 alone": ("4442f800 " + JR_RA, "fcsr"),
    "k0 jump target": ("3c1a8011 275a16a0 03400008 00000000", "kreg"),
    "k1 read": ("03601025 " + JR_RA, "kreg"),
}


class OriginalRuleTests(unittest.TestCase):
    def test_probed_compiler_output_is_never_original(self) -> None:
        for label, functions in PROBE.items():
            own = emitters([ADAPTERS[label]])
            for name, text in functions.items():
                for emitted, scope in ((own, "own"), (GAMES, "games")):
                    with self.subTest(compiler=label, function=name, emitted=scope):
                        self.assertIsNone(shape.original(words(text), emitted))

    def test_compiler_idioms_stay_c(self) -> None:
        for label, (text, emitted) in C_IDIOMS.items():
            with self.subTest(label):
                self.assertIsNone(shape.original(words(text), emitted))

    def test_near_misses_are_not_original(self) -> None:
        for label, text in NEAR_MISSES.items():
            with self.subTest(label):
                self.assertIsNone(shape.original(words(text), GAMES))
                route = shape.classify(bytes.fromhex(text.replace(" ", "")), 0x80001000, IDO_ONLY, GAMES)[0]
                self.assertNotEqual(route, "original")

    def test_each_rule_fires_on_its_instruction(self) -> None:
        for label, (text, rule) in POSITIVES.items():
            with self.subTest(label):
                found = shape.original(words(text), GAMES)
                self.assertIsNotNone(found)
                assert found is not None
                self.assertEqual(found.rule, rule)
                self.assertIn("+0x", found.evidence)

    def test_isa_fires_only_above_every_configured_level(self) -> None:
        body = words("0004103c 03e00008 0002103f")
        found = shape.original(body, IDO_ONLY)
        self.assertEqual(found.rule if found else None, "isa")
        self.assertIsNone(shape.original(body, GAMES))

    def test_a_rule_holds_only_where_every_family_switches_it_on(self) -> None:
        for rule, (text, _) in {"cop0": POSITIVES["mtc0"], "fcsr": POSITIVES["cfc1 $31 alone"]}.items():
            with self.subTest(rule):
                off = dataclasses.replace(ADAPTERS["ido-7.1 -mips2"], rules=ADAPTERS["ido-7.1 -mips2"].rules - {rule})
                self.assertIsNone(shape.original(words(text), emitters([ADAPTERS["gcc-2.7.2-kmc -mips3"], off])))

    def test_rule_names_are_a_closed_set_every_adapter_switches_on(self) -> None:
        self.assertEqual(ORIGINAL_RULES, frozenset({"cop0", "fcsr", "kreg", "isa"}))
        self.assertEqual(GAMES.rules, ORIGINAL_RULES)
        for label, adapter in ADAPTERS.items():
            with self.subTest(label):
                self.assertLessEqual(ORIGINAL_RULES, adapter.rules)

    def test_classify_routes_original_before_counting_returns(self) -> None:
        # BT osInvalICache: two jr ra in one row, cache inside.
        body = "bd100000 " + JR_RA + " 3c088000 bd000000 " + JR_RA
        self.assertEqual(
            shape.classify(bytes.fromhex(body.replace(" ", "")), 0x80112570, IDO_ONLY, GAMES)[0], "original"
        )


if __name__ == "__main__":
    unittest.main()
