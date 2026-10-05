"""pool.run(shared=...), worker failures that name their place, per-kind cache counts and the sizing rule."""

import unittest
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any
from unittest.mock import patch

from tests.kit import TempCase
from unbake import cache, effort, pool
from unbake.config import Held

GIB = 1 << 30


def add(shared: dict[str, int], item: int) -> int:
    return shared["base"] + item


def explode(item: int) -> int:
    return int("not a number")


def counting(item: int) -> int:
    effort.count("cache.sample", item, 1)
    return item


def serial(self: pool.Pool, fn: Any, jobs: Sequence[Any], *, charge: str | None = None) -> Iterator[Any]:
    for job in jobs:
        yield fn(job)


class SharedTests(TempCase):
    def test_workers_get_the_value_and_the_file_is_gone_afterwards(self) -> None:
        scratch = self.root / "scratch"
        workers = pool.Pool(2, 64 * GIB, GIB, GIB, scratch)
        seen: list[Path] = []
        original = pool._shared_value

        def spy(path: str) -> Any:
            seen.append(Path(path))
            self.assertTrue(Path(path).is_file())
            return original(path)

        with patch.object(pool.Pool, "map", serial), patch.object(pool, "_shared_value", spy):
            result = workers.run(add, list(range(30)), {"base": 100})
        self.assertEqual(result, [100 + value for value in range(30)])
        self.assertEqual(len(set(seen)), 1)
        self.assertEqual(list(scratch.iterdir()), [])

    def test_a_shared_value_needs_a_scratch_directory(self) -> None:
        with self.assertRaises(Held) as raised:
            pool.Pool(2, 64 * GIB, GIB, GIB).run(add, [1, 2], {"base": 1})
        self.assertEqual(raised.exception.key, "pool.shared")

    def test_sizing_is_one_rule(self) -> None:
        self.assertEqual([pool.width(n, 4, 1) for n in (0, 1, 4, 9)], [1, 1, 1, 3])
        self.assertEqual(pool.width(100, 5), 5)


class MeasuredTests(unittest.TestCase):
    def test_a_crash_becomes_a_refusal_naming_the_function_and_place(self) -> None:
        with self.assertRaises(Held) as raised:
            pool._measured((explode, 1))
        text = raised.exception.reason
        self.assertIn("explode", text)
        self.assertIn("ValueError at test_pool_shared.py:", text)
        self.assertIn("not a number", text)

    def test_a_refusal_and_memory_pass_as_themselves(self) -> None:
        def refuse(item: int) -> int:
            raise Held("x", "x.key: reason")

        def starve(item: int) -> int:
            raise MemoryError

        with self.assertRaises(Held) as raised:
            pool._measured((refuse, 1))
        self.assertEqual(raised.exception.key, "x.key")
        with self.assertRaises(pool.WorkerMemory):
            pool._measured((starve, 1))

    def test_worker_counts_reach_the_main_ledger(self) -> None:
        _, _, _, added = pool._measured((counting, 2))
        self.assertEqual(added, {"cache.sample": (2, 1)})
        before = effort.counted().get("cache.sample", (0, 0))
        effort.charge("n", 0.0, 0, added)
        after = effort.counted()["cache.sample"]
        self.assertEqual((after[0] - before[0], after[1] - before[1]), (2, 1))


class CacheCountTests(TempCase):
    def test_a_hit_and_a_miss_are_counted_per_kind(self) -> None:
        store = cache.Cache(self.root)
        key = cache.key("a")
        start = effort.mark()
        self.assertIsNone(store.get("kind-one", key))
        store.produce("kind-one", key, lambda path: Path(path).touch())
        store.produce("kind-one", key, lambda path: self.fail("made twice"))
        self.assertIsNotNone(store.get("kind-one", key))
        self.assertEqual(effort.since(start).counts["cache.kind-one"], (2, 4))


if __name__ == "__main__":
    unittest.main()
