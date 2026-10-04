"""Cycle ranking: bytes per effort, carryovers first, size window, pooled rates for sparse buckets."""

import unittest

from unbake.cycle.rank import Candidate, History, rank

WINDOW = {"min_bytes": 16, "max_bytes": 4096, "min_history": 3}


def cand(name: str, size: int, carryover: bool = False) -> Candidate:
    return Candidate(name, size, ("us",), carryover, 0.0)


def attempts(size: int, exact: list[bool], minutes: float) -> list[History]:
    return [History(f"h{size}-{i}", size, flag, minutes) for i, flag in enumerate(exact)]


def names(result: list[Candidate]) -> list[str]:
    return [item.function for item in result]


class RankTests(unittest.TestCase):
    def test_no_history_ranks_by_size_descending_inside_the_window(self) -> None:
        pool = [cand("small", 32), cand("big", 2048), cand("mid", 256), cand("tiny", 8), cand("huge", 9000)]
        self.assertEqual(names(rank(pool, [], **WINDOW)), ["big", "mid", "small"])

    def test_window_edges_are_inclusive(self) -> None:
        pool = [cand("low", 16), cand("high", 4096), cand("under", 15), cand("over", 4097)]
        self.assertEqual(names(rank(pool, [], **WINDOW)), ["high", "low"])

    def test_carryovers_come_first_even_when_smaller(self) -> None:
        pool = [cand("fresh-big", 2048), cand("old-small", 32, True), cand("old-mid", 256, True), cand("fresh", 512)]
        result = names(rank(pool, [], **WINDOW))
        self.assertEqual(result[:2].count("old-small") + result[:2].count("old-mid"), 2)
        self.assertEqual(result[2:], ["fresh-big", "fresh"])

    def test_history_prefers_bytes_per_effort_over_size(self) -> None:
        history = attempts(100, [True] * 5, 10.0) + attempts(300, [True] + [False] * 4, 100.0)
        pool = [cand("quick-win", 100), cand("slow-big", 300)]
        self.assertEqual(names(rank(pool, history, **WINDOW)), ["quick-win", "slow-big"])

    def test_sparse_bucket_uses_the_pooled_rate(self) -> None:
        history = attempts(100, [True] * 5, 1.0) + attempts(1000, [False] * 8, 1.0)
        pool = [cand("proven", 100), cand("hopeless", 1000), cand("unknown", 100 + 28)]
        # 128 falls in a bucket with no samples, so it scores with the pooled rate: below the proven
        # bucket (p = 1) and above the bucket that never matched (p = 0).
        result = names(rank(pool, history, **WINDOW))
        self.assertEqual(result[0], "proven")
        self.assertEqual(result[-1], "hopeless")

    def test_bucket_is_floor_log2(self) -> None:
        # 127 and 64 share a bucket (6); 128 starts the next one, which has no history of its own and
        # so scores with the pooled rate, which the failing bucket drags below bucket 6.
        history = attempts(64, [True] * 3, 1.0) + attempts(127, [True] * 3, 1.0) + attempts(1024, [False] * 6, 1.0)
        pool = [cand("a", 127), cand("b", 128)]
        self.assertEqual(names(rank(pool, history, **WINDOW))[0], "a")

    def test_ranking_keeps_the_candidates_and_is_deterministic(self) -> None:
        pool = [cand("a", 64), cand("b", 64), cand("c", 64)]
        first, second = rank(pool, [], **WINDOW), rank(list(reversed(pool)), [], **WINDOW)
        self.assertEqual(sorted(names(first)), ["a", "b", "c"])
        self.assertEqual(names(first), names(second))
