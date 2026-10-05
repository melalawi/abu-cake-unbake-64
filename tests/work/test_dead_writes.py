"""work.shape zero_write and dead_write: words an optimizing compiler never emits route to "dead"."""

import unittest

from unbake.compilers.families.gcc import Gcc
from unbake.compilers.families.ido import Ido
from unbake.compilers.families.mips import Shape, emitters
from unbake.work import shape

O2 = Gcc().shape("gcc-2.8.1-sn64", ("-G0", "-mips3", "-O2", "-mgp32", "-mfp64"))
O0 = Gcc().shape("gcc-2.8.1-sn64", ("-G0", "-mips3", "-O0", "-mgp32", "-mfp64"))
BARE = Gcc().shape("gcc-2.8.1-sn64", ("-G0", "-mips3"))
IDO = Ido().shape("ido-7.1", ("-c", "-G0", "-non_shared", "-mips2", "-O2"))
EMITTED = emitters([O2])
JR = "03e00008"


def route(text: str, target: Shape = O2) -> tuple[str, str]:
    return shape.classify(bytes.fromhex(text.replace(" ", "")), 0x80001000, target, EMITTED)


class DeadWrites(unittest.TestCase):
    def test_real_words_from_the_games(self) -> None:
        cases = {
            "func_8040C638_us writes $zero": ("940f000a 00040000 " + JR + " 00001021", "dead", "writes $zero"),
            "func_8043C314_de a0 is written and never read": (
                "3c020000 24420000 " + JR + " 2484ffff",
                "dead",
                "writes $4 and never reads it",
            ),
            "func_8023CFD8_eu two stores are C": ("ac800028 ac80002c " + JR + " 00000000", "drafter", ""),
        }
        for name, (text, kind, evidence) in cases.items():
            with self.subTest(name):
                got = route(text)
                self.assertEqual(got[0], kind, got[1])
                self.assertIn(evidence, got[1])

    def test_near_misses_stay_with_the_drafter(self) -> None:
        cases = {
            "a plain nop": JR + " 00000000",
            "a real return value": JR + " 24020001",
            "v1 is a result too": JR + " 24030001",
            "a write the delay slot reads": "2484ffff " + JR + " 00801021",
            "a write a later instruction reads": "24080001 01001021 " + JR + " 00000000",
            "a restore of s0": "8fb00000 " + JR + " 00000000",
            "f0 is the float result": "44840000 " + JR + " 00000000",
            "a branch may read it elsewhere": "24080001 10800001 00000000 " + JR + " 01001021",
        }
        for name, text in cases.items():
            with self.subTest(name):
                self.assertEqual(route(text)[0], "drafter", route(text)[1])

    def test_more_dead_forms(self) -> None:
        cases = {
            "overwritten before it is read": "24080001 24080002 01001021 " + JR + " 00000000",
            "an s0 write that is not a load": "26100001 " + JR + " 00000000",
            "an fpr nothing reads": "44842000 " + JR + " 00000000",
            "mfc1 into $zero": "44000000 " + JR + " 00000000",
        }
        for name, text in cases.items():
            with self.subTest(name):
                self.assertEqual(route(text)[0], "dead", route(text)[1])

    def test_a_longer_body_is_never_judged(self) -> None:
        self.assertEqual(route("2484ffff " * 16 + JR + " 00000000")[0], "drafter")

    def test_only_an_optimizing_compiler_has_the_rules(self) -> None:
        for target in (O0, BARE):
            self.assertFalse({"zero_write", "dead_write"} & target.rules)
            self.assertEqual(route("940f000a 00040000 " + JR + " 00001021", target)[0], "drafter")
        for target in (O2, IDO):
            self.assertTrue({"zero_write", "dead_write"} <= target.rules)

    def test_a_rule_can_be_switched_off_alone(self) -> None:
        import dataclasses

        no_dead = dataclasses.replace(O2, rules=O2.rules - {"dead_write"})
        no_zero = dataclasses.replace(O2, rules=O2.rules - {"zero_write"})
        self.assertEqual(route("3c020000 24420000 " + JR + " 2484ffff", no_dead)[0], "drafter")
        self.assertEqual(route("940f000a 00040000 " + JR + " 00001021", no_zero)[0], "dead")  # the t7 write is dead
        self.assertIn("writes $zero", route("00040000 " + JR + " 00001021", no_dead)[1])


if __name__ == "__main__":
    unittest.main()
