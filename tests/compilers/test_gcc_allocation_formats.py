"""Verbatim allocator rows from the failing draft, compiled by both pinned GCCs."""

import unittest
from pathlib import Path

from unbake.compilers.families.gcc.allocation import allocation, global_priority
from unbake.config import Held

FIXTURES = Path(__file__).with_name("fixtures")


class DumpFormats(unittest.TestCase):
    def test_real_multiword_order_rows(self) -> None:
        for compiler, count, first, refs, live in (
            ("gcc-2.7.2-kmc", 217, 73, 224, 335),
            ("gcc-2.8.1-sn64", 292, 81, 224, 335),
        ):
            with self.subTest(compiler=compiler):
                result = allocation(
                    {suffix: (FIXTURES / f"{compiler}.{suffix}").read_text() for suffix in ("lreg", "greg")}
                )
                ranked = sorted(result.pseudos, key=lambda p: p.rank)
                self.assertEqual(len(ranked), count)
                self.assertEqual((ranked[0].number, ranked[0].references, ranked[0].live_length), (first, refs, live))
                self.assertEqual(ranked[0].words, 2)
                self.assertEqual(ranked[1].words, 2)
                self.assertEqual(ranked[2].words, 1)

    def test_real_setjmp_lifetime_sentinels_are_preserved(self) -> None:
        for compiler, number in (("gcc-2.7.2-kmc", 74), ("gcc-2.8.1-sn64", 82)):
            with self.subTest(compiler=compiler):
                result = allocation(
                    {suffix: (FIXTURES / f"{compiler}-sentinel.{suffix}").read_text() for suffix in ("lreg", "greg")}
                )
                pseudo = next(p for p in result.pseudos if p.number == number)
                self.assertEqual(
                    (
                        pseudo.references,
                        pseudo.live_length,
                        pseudo.hard,
                        pseudo.priority,
                        pseudo.rank,
                        pseudo.allocator,
                    ),
                    (3, -1, None, None, None, "unallocated"),
                )

    def test_plain_wrapped_dispositions_and_numeric_lists(self) -> None:
        result = allocation(
            {
                "lreg": "Register 60 used 3 times across 5 insns in block 4; pointer.\n"
                "Register 61 used 7 times across 9 insns; 8 bytes.\n",
                "greg": ";; 2 regs to allocate: 61 (2)\t60\n"
                ";; 60 conflicts: 61 2 29\n;; 61 conflicts:\n"
                ";; 60 preferences: 2\n"
                ";; Need 1 reg of class GR_REGS (for insn 374).\n"
                ";; Need 2 regs of class ALL_REGS (for insn 374).\n"
                "Spilling reg 15.\n;; Register dispositions:\n"
                "60 in 2  61 in 16\n\n;; Hard regs used: 2 16 17\n(note 1)\n",
            }
        )
        first, second = result.pseudos
        self.assertEqual(
            (first.number, first.hard, first.references, first.live_length, first.rank, first.conflicts, first.words),
            (60, 2, 3, 5, 1, (61, 2, 29), 1),
        )
        self.assertEqual((second.number, second.hard, second.rank, second.conflicts, second.words), (61, 16, 0, (), 2))
        self.assertEqual(second.priority, global_priority(7, 9, 2))
        self.assertEqual(result.hard_registers, (2, 16))

    def test_native_summary_and_reload_template_variants(self) -> None:
        result = allocation(
            {
                "lreg": ";; Function f\n72 registers.\nRegister 60 used 3 times across 5 insns.\n"
                "1 basic blocks.\nBasic block 0: first insn 1, last 5.\n"
                "Reached from blocks:  previous\nRegisters live at start: 4 29 60\n"
                ";; Register 60 in 2.\n(note 1)\n;; RTL diagnostics follow\n",
                "greg": ";; 1 regs to allocate: 60\n"
                ";; Need 1 nongroup reg of class GR_REGS (for insn 374).\n"
                ";; Need 2 nongroup regs of class GR_REGS (for insn 374).\n"
                ";; Need 1 group (DImode) of class GR_REGS (for insn 374).\n"
                ";; Need 2 groups (DFmode) of class FP_REGS (for insn 374).\n"
                " Register 60 now on stack.\n Register 60 now in 2.\n"
                ";; Register dispositions:\n60 in 2\n\n;; Hard regs used: 2 29\n(note 1)\n",
            }
        )
        self.assertEqual(
            (result.pseudos[0].hard, result.pseudos[0].references, result.pseudos[0].live_length), (2, 3, 5)
        )

    def test_joined_allocnos_share_rank_and_retain_each_usage(self) -> None:
        result = allocation(
            {
                "lreg": "Register 60 used 3 times across 5 insns\n"
                "Register 61 used 7 times across 9 insns\nRegister 62 used 2 times across 3 insns\n",
                "greg": ";; 2 regs to allocate: 60+61 (2) 62\n;; Register dispositions:\n60 in 2 61 in 2\n",
            }
        )
        first, second, third = result.pseudos
        self.assertEqual((first.rank, second.rank, third.rank), (0, 0, 1))
        self.assertEqual((first.references, first.live_length, second.references, second.live_length), (3, 5, 7, 9))
        self.assertEqual((first.words, second.words, third.words), (2, 2, 1))
        self.assertEqual((first.priority, second.priority), (global_priority(10, 9, 2), global_priority(10, 9, 2)))
        self.assertEqual(global_priority(224, 335, 2), 93611)

    def test_local_decision_fields_and_large_signed_priority_are_exact(self) -> None:
        result = allocation(
            {
                "lreg": "Register 60 used 3 times across 5 insns\nRegister 61 used 4 times across 7 insns\n",
                "greg": ";; Register dispositions:\n60 in 2  61 in 3\n\n;; ",
                "lalloc": ";; Block 4:\n"
                ";; qty 7 pseudo 60 61 SI size 2 refs 17 calls 3 class GR_REGS alternate NO_REGS "
                "life 11-29 priority -9007199254740993\n"
                ";; qty 7 wants 2\n;; 8 rejected: overlaps live hard register\n"
                ";; pseudo 60 in 2 (qty 7, offset -1)\n"
                ";; pseudo 61 in 3 (qty 7, offset 0)\n",
            }
        )
        for pseudo in result.pseudos:
            self.assertEqual(
                (
                    pseudo.references,
                    pseudo.live_length,
                    pseudo.live_range,
                    pseudo.priority,
                    pseudo.words,
                    pseudo.rejections,
                ),
                (17, 18, (11, 29), -9007199254740993, 2, ((8, "overlaps live hard register"),)),
            )

    def test_global_decision_fields_and_large_priority_are_exact(self) -> None:
        result = allocation(
            {
                "lreg": "Register 60 used 3 times across 5 insns\nRegister 61 used 4 times across 7 insns\n",
                "greg": ";; 2 regs to allocate: 60 (2) 61\n;; Register dispositions:\n60 in 2  61 in 3\n",
                "galloc": ";; allocno 7 pseudo 60 61 SI size 2 refs 17 live_length 18 calls 3\n"
                ";; allocno 7 pseudo 60 live range runs from insn 11 to insn 29\n"
                ";; allocno 7 priority log2(refs)*refs/live = 9007199254740993\n"
                ";; allocno 7 seeking 2\n;; 8 rejected: conflict\n",
            }
        )
        for pseudo in result.pseudos:
            self.assertEqual(
                (pseudo.references, pseudo.live_length, pseudo.priority, pseudo.words, pseudo.rejections),
                (17, 18, 9007199254740993, 2, ((8, "conflict"),)),
            )
        self.assertEqual(result.pseudos[0].live_range, (11, 29))

    def test_unknown_rows_are_named_in_every_stream(self) -> None:
        base = {
            "lreg": "Register 60 used 3 times across 5 insns\n",
            "greg": ";; 1 regs to allocate: 60\n;; Register dispositions:\n60 in 2\n\n;; ",
        }
        cases = [
            ("lreg", ";; new local allocation decision"),
            ("lreg", "Register x used 3 times across 5 insns"),
            ("lreg", "Register 60 used (3) times across 5 insns"),
            ("lreg", "Register 60 used 3 times across ? insns"),
            ("greg", ";; 1 regs to allocate: (2) 60"),
            ("greg", ";; 1 regs to allocate: 60 (bogus)"),
            ("greg", ";; 60 conflicts: 61 unknown"),
            ("greg", ";; 60 preferences: ?"),
            ("greg", ";; 60 new allocator decision: unknown"),
            ("lalloc", ";; Block unknown:"),
            (
                "lalloc",
                ";; qty 7 pseudo 60 SI size ? refs 3 calls 0 class GR_REGS alternate NO_REGS life 1-5 priority 6000",
            ),
            (
                "lalloc",
                ";; qty 7 pseudo 60 SI size 1 refs ? calls 0 class GR_REGS alternate NO_REGS life 1-5 priority 6000",
            ),
            ("lalloc", ";; pseudo 60 in 2 (qty 7, offset ?)"),
            ("galloc", ";; allocno 7 pseudo 60 SI size 1 refs 3 live_length ? calls 0"),
            ("galloc", ";; allocno 7 priority refs/live = unknown"),
            ("galloc", ";; allocno 7 pseudo 60 live range runs from insn ? to insn 5"),
        ]
        for stream, line in cases:
            with self.subTest(stream=stream, line=line):
                dumps = dict(base)
                dumps[stream] = line if stream == "lreg" else line + "\n" + dumps.get(stream, "")
                with self.assertRaises(Held) as raised:
                    allocation(dumps)
                self.assertEqual(raised.exception.key, f"dumps.{stream}.unknown_line")
                self.assertIn(line, raised.exception.reason)
        with self.assertRaisesRegex(Held, "dumps.greg.unknown_line"):
            allocation({**base, "greg": ";; Register dispositions:\n60 in unknown\n"})

    def test_inconsistent_rank_counts_are_still_named(self) -> None:
        for order in (";; 2 regs to allocate: 60", ";; 2 regs to allocate: 60 60"):
            with self.subTest(order=order), self.assertRaisesRegex(Held, "dumps.greg.order"):
                allocation(
                    {
                        "lreg": "Register 60 used 3 times across 5 insns",
                        "greg": order + "\n;; Register dispositions:\n60 in 2\n",
                    }
                )


if __name__ == "__main__":
    unittest.main()
