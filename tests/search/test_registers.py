import time
import unittest
from types import SimpleNamespace

from unbake.decomp.explain import Allocation, align, annotate, function_dump, leverage, render
from unbake.decomp.trial_compare import compare_words
from unbake.families.gcc.allocation import allocation as gcc
from unbake.families.gcc.allocation import dump_flags as gcc_flags
from unbake.families.gcc.allocation import flip, global_priority
from unbake.families.ido.allocation import allocation as ido
from unbake.families.ido.allocation import dump_flags as ido_flags
from unbake.project.config import Held
from unbake.search.registers import propose

LREG = """Register 80 used 270 times across 2490 insns; dies in 5 places; crosses 79 calls; GR_REGS or none; pointer.
Register 81 used 4 times across 46 insns; crosses 3 calls; GR_REGS or none; pointer.
Register 82 used 292 times across 790 insns; crosses 60 calls; GR_REGS or none; pointer.
"""
GREG = """;; 3 regs to allocate: 82 80 81
;; 80 conflicts: 81 82 2 29
;; Register dispositions:
80 in 19 81 in 17 82 in 16
;; Hard regs used: 16 17 19
"""


def evidence() -> Allocation:
    return gcc({"lreg": LREG, "greg": GREG})


class RegistersTests(unittest.TestCase):
    def test_family_evidence(self) -> None:
        for family, dumps, hard in (
            (gcc, {"lreg": LREG, "greg": GREG}, (16, 17, 19)),
            (ido, {"ido": "lw $2,24($16)\nadd.s $f0,$f2,$f4"}, (2, 16)),
        ):
            with self.subTest(family=family.__module__):
                result = family(dumps)
                self.assertEqual(result.hard_registers, hard)
                if family is gcc:
                    self.assertEqual(result.pseudos[0].priority, 8674)
                    self.assertEqual(result.pseudos[2].rank, 0)
                else:
                    self.assertFalse(result.pseudos)
                    self.assertIn("unavailable", result.limitations[0])
        self.assertEqual(gcc_flags(), ("-da",))
        self.assertEqual(ido_flags(), ("-K", "-S"))

    def test_spill_is_absent_disposition(self) -> None:
        for placed, missing in (("80 in 19 81 in 17", 82), ("80 in 19 82 in 16", 81)):
            with self.subTest(missing=missing):
                result = gcc(
                    {"lreg": LREG, "greg": GREG.replace("80 in 19 81 in 17 82 in 16", placed) + "\nSpilling reg 9.\n"}
                )
                self.assertIsNone(next(p for p in result.pseudos if p.number == missing).hard)
                self.assertNotIn(9, [p.number for p in result.pseudos])

    def test_named_refusals(self) -> None:
        rows = [
            (gcc, {}, "dumps.lreg"),
            (gcc, {"lreg": LREG}, "dumps.greg"),
            (gcc, {"lreg": "noise", "greg": GREG}, "usage"),
            (gcc, {"lreg": LREG.replace("270", "0"), "greg": GREG}, "references/live_length"),
            (gcc, {"lreg": LREG, "greg": GREG.replace("Register dispositions:", "noise:")}, "dispositions"),
            (gcc, {"lreg": LREG, "greg": GREG.replace("3 regs", "4 regs")}, "order"),
            (gcc, {"lreg": LREG, "greg": GREG.replace("82 in 16", "83 in 16")}, "pseudo.83.usage"),
            (gcc, {"lreg": LREG, "greg": GREG, "lalloc": ";; qty 1 wants a SI"}, "quantity"),
            (gcc, {"lreg": LREG, "greg": GREG, "lalloc": ";;   19 rejected: live"}, "request"),
            (gcc, {"lreg": LREG, "greg": GREG, "galloc": ";; allocno 5 priority x = 4"}, "allocno.5"),
            (gcc, {"lreg": LREG, "greg": GREG, "galloc": ";;   19 rejected: live"}, "request"),
            (ido, {}, "dumps.ido"),
            (ido, {"ido": "noise"}, "assignments"),
            (ido, {"ido": "lw $32,0($1)"}, "general register"),
        ]
        for family, dumps, name in rows:
            with (
                self.subTest(name=name, family=family.__module__),
                self.assertRaisesRegex(Held, name.replace(".", r"\.")),
            ):
                family(dumps)

    def test_diagnostic_local_quantity_and_global_priority(self) -> None:
        local = """;; Block 7: 1 quantity over 90 insns
;; qty 0 pseudo 81 SI size 1 refs 4 calls 0 class GR_REGS alternate NO_REGS life 2-48 priority 869
;; qty 0 wants a SI in class GR_REGS over 2-48
;;   19 rejected: live over 2-48
;; pseudo 81 in 17 (qty 0, offset 0)
"""
        global_log = """\
;; allocno 2 pseudo 80 SI size 1 refs 270 live_length 2490 calls 79 class GR_REGS alternate NO_REGS
;; allocno 2 priority = floor_log2(270) 8 * refs 270 / live_length 2490 * 10000 * size 1 = 8674
;; allocno 2 pseudo 80 live range runs from insn 121 to insn 429
;; allocno 2 (pseudo 80) seeking a SI in class GR_REGS
;;   16 rejected: conflicts with allocno 1 (pseudo 82)
"""
        result = gcc({"lreg": LREG, "greg": GREG, "lalloc": local, "galloc": global_log})
        self.assertEqual(result.pseudos[0].priority, 8674)
        self.assertEqual(result.pseudos[0].live_range, (121, 429))
        self.assertEqual(result.pseudos[1].live_range, (2, 48))
        self.assertEqual(result.pseudos[1].rejections, ((19, "live over 2-48"),))

    def test_global_priority_matches_gcc_and_names_the_flip(self) -> None:
        # RageWars func_8044D408: k (17 refs over 144 insns) is allocated just before the slot
        # pointer (11 refs over 70 insns), so the pointer loses s7. Real log2 would rank them the
        # other way round; GCC uses floor_log2.
        self.assertEqual((global_priority(17, 144), global_priority(11, 70)), (4722, 4714))
        self.assertEqual(
            flip((11, 70), (17, 144)),
            "candidate live_length <= 69; candidate references >= 12; holder live_length >= 145",
        )
        self.assertEqual(flip((1, 5), (17, 144)), "candidate references >= 3")
        with self.assertRaisesRegex(Held, "priority"):
            global_priority(0, 5)

    def test_function_sections(self) -> None:
        for text, name, expected in [
            (";; Function a\nX\n;; Function b\nY\n", "b", "Y"),
            ("noise", "a", None),
            (";; Function a\nX\n;; Function a\nY", "a", None),
        ]:
            with self.subTest(name=name, text=text):
                if expected is None:
                    with self.assertRaisesRegex(Held, "dumps.function"):
                        function_dump(text, name)
                else:
                    self.assertEqual(function_dump(text, name).strip(), expected)

    def test_alignment_winners_and_no_gain(self) -> None:
        for name, target, draft, expected in [
            ("func_80220EB0", [0x8E020018], [0x8E620018], 1),
            ("func_8021EED8", [0x02201021], [0x02601021], 1),
            ("func_8027DD1C", [0x03E00008, 0], [0x03E00008, 0], 0),
        ]:
            with self.subTest(function=name):
                result = align(evidence(), compare_words("us", target, draft))
                self.assertEqual(len(result.differences), expected)
                if expected:
                    self.assertEqual(result.differences[0].candidates, (80,))
                    self.assertFalse(result.differences[0].ambiguous)
                    self.assertIn("holder pseudo", render(result))
                    self.assertIn("allocation order: pseudo", render(result))
                else:
                    self.assertIn("No aligned", render(result))

    def test_source_mapping_and_leverage(self) -> None:
        rtl = '(note 1 0 2 ("source.i") 1)\n(insn 2 1 3 (set (reg/v:SI 80) (const_int 1)))'
        result = annotate(evidence(), rtl, "a = 1;\n", "a = 1;\n")
        self.assertEqual(leverage(result)[0].name, "a")
        self.assertEqual(leverage(result)[0].source_lines, (1,))
        self.assertFalse(leverage(annotate(evidence(), rtl, "a = 1;\na = 1;", "a = 1;")))

    def test_mutations_and_safety(self) -> None:
        rows = [
            ("int f(void) {\n int a;\n int b;\n a=1;\n use(a);\n b=2;\n return b;\n}", "registers.recycle"),
            ("int f(void) {\n int a;\n a=1;\n use(a);\n a=2;\n return a;\n}", "registers.split"),
            ("int f(void) {\n int a;\n int b;\n a=1;\n b=a;\n return b;\n}", "registers.merge"),
            ("int f(void) {\n int a;\n int b;\n a=1;\n use(a);\n b=2;\n return b;\n}", "registers.reorder"),
        ]
        for source, kind in rows:
            with self.subTest(kind=kind):
                mutations = list(
                    propose(source, object(), SimpleNamespace(allocation=evidence(), deadline=time.monotonic() + 30))
                )
                self.assertIn(kind, [m.kind for m in mutations])
                self.assertEqual(len({m.source for m in mutations}), len(mutations))
        for statement in ("if (a) b=2;", "use(&a); b=2;", "while(a) b=2;", "b=a+1; use(a);"):
            with self.subTest(statement=statement):
                source = "int f(void) {\n int a;\n int b;\n a=1;\n use(a);\n " + statement + "\n return b;\n}"
                mutations = list(
                    propose(source, object(), SimpleNamespace(allocation=evidence(), deadline=time.monotonic() + 30))
                )
                self.assertNotIn("registers.recycle", [m.kind for m in mutations])
        for source, trial, ctx, name in [
            ("", object(), SimpleNamespace(allocation=evidence(), deadline=time.monotonic() + 30), "source"),
            ("int f(){}", None, SimpleNamespace(allocation=evidence(), deadline=time.monotonic() + 30), "trial"),
            ("int f(){}", object(), None, "context.allocation"),
        ]:
            with self.subTest(name=name), self.assertRaisesRegex(Held, name):
                list(propose(source, trial, ctx))


if __name__ == "__main__":
    unittest.main()
