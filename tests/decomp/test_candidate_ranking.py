"""Exact evidence wins over a prettier fuzzy score in flag probes too."""

import unittest

from unbake.decomp.candidate_ranking import candidate_rank
from unbake.decomp.trial_compare import TYPES, Compare
from unbake.decomp.trial_flags import FlagResult, ranking


class CandidateRankingTests(unittest.TestCase):
    def test_each_evidence_priority(self) -> None:
        cases = (
            ((True, 1, 0, {"us": 0}), (False, 100, 0, {"us": 100})),
            ((False, 11, 10, {"us": 20}), (False, 10, 0, {"us": 100})),
            ((False, 11, 1, {"us": 20}), (False, 11, 2, {"us": 100})),
            ((False, 11, 1, {"us": 30, "eu": 40}), (False, 11, 1, {"us": 100, "eu": 20})),
        )
        for better, worse in cases:
            with self.subTest(better=better, worse=worse):
                self.assertLess(candidate_rank(*better), candidate_rank(*worse))

    def test_flag_probe_prefers_words_then_differences_and_ranks_failure_last(self) -> None:
        def result(flags: tuple[str, ...], words: int, changes: int, score: float) -> FlagResult:
            typed = dict.fromkeys(TYPES, 0)
            typed["changed"] = changes
            return FlagResult(flags, {"us": Compare("us", words, 10, typed, [], score, ())}, {})

        variants = [
            result((), 8, 1, 99),
            result(("-O1",), 9, 2, 95),
            result(("-O2",), 9, 1, 90),
            FlagResult(("-broken",), {}, {"us": "compiler error"}),
        ]
        lines = ranking(variants, ["us"])
        self.assertIn("VERSION us flags 1: -O2; objdiff 90.000000%; BEATS PROJECT FLAGS", lines)
        self.assertIn("VERSION us flags 2: -O1; objdiff 95.000000%; BEATS PROJECT FLAGS", lines)
        self.assertIn("VERSION us flags 4: -broken; compile failed: compiler error", lines)


if __name__ == "__main__":
    unittest.main()
