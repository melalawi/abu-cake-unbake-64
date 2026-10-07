"""The content-keyed cache: key framing, single-flight, failure, trim and refusals."""

import os
import threading
from pathlib import Path

from tests.kit import TempCase
from unbake import cache
from unbake.config import Held

H = "ab" + "0" * 62


def write(path: Path, size: int) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    return path


class KeyTests(TempCase):
    def test_framing_separates_part_boundaries(self) -> None:
        self.assertNotEqual(cache.key("ab", "c"), cache.key("a", "bc"))
        self.assertNotEqual(cache.key("a", "b"), cache.key("ab"))
        self.assertNotEqual(cache.key("a", ""), cache.key("", "a"))
        self.assertEqual(cache.key("a"), cache.key(b"a"))
        self.assertRegex(cache.key("a"), r"^[0-9a-f]{64}$")

    def test_path_parts_are_refused_until_the_caller_names_their_content(self) -> None:
        from unbake import inputs

        first = write(self.root / "one.bin", 3)
        second = write(self.root / "two.bin", 3)
        with self.assertRaises(Held):
            cache.key(first)
        self.assertEqual(
            cache.key(inputs.digest(first, algorithm="sha256", reuse=True)),
            cache.key(inputs.digest(second, algorithm="sha256", reuse=True)),
        )

    def test_bad_parts_are_refused(self) -> None:
        for label, part in [("integer", 3), ("missing file", self.root / "absent")]:
            with self.subTest(label), self.assertRaises(Held):
                cache.key(part)


class StoreTests(TempCase):
    def setUp(self) -> None:
        super().setUp()
        self.store = cache.Cache(self.root / "cache")

    def test_path_layout(self) -> None:
        self.assertEqual(self.store.path("obj", H), self.root / "cache" / "obj" / "ab" / H)

    def test_refuses_bad_kind_and_key(self) -> None:
        for kind, key in [("", H), ("../x", H), ("a/b", H), ("obj", "zz"), ("obj", H.upper()), ("obj", H[:-1])]:
            with self.subTest(kind=kind, key=key), self.assertRaises(Held):
                self.store.path(kind, key)

    def test_get_put_roundtrip_and_directory_entries(self) -> None:
        self.assertIsNone(self.store.get("obj", H))
        stored = self.store.put("obj", H, write(self.root / "src.bin", 5))
        self.assertEqual(self.store.get("obj", H), stored)
        self.assertEqual(stored.read_bytes(), b"xxxxx")
        other = "cd" + "0" * 62
        self.store.path("obj", other).mkdir(parents=True)
        with self.assertRaises(Held):
            self.store.get("obj", other)

    def test_put_refuses_non_files(self) -> None:
        for label, source in [("missing", self.root / "absent"), ("directory", self.root)]:
            with self.subTest(label), self.assertRaises(Held):
                self.store.put("obj", H, source)

    def test_produce_publishes_once_then_hits(self) -> None:
        made = []

        def make(target: Path) -> None:
            made.append(target)
            target.write_bytes(b"made")

        first = self.store.produce("pre", H, make)
        second = self.store.produce("pre", H, make)
        self.assertEqual((first, first.read_bytes()), (second, b"made"))
        self.assertEqual(len(made), 1)

    def test_failed_make_publishes_nothing_and_next_caller_computes(self) -> None:
        def broken(target: Path) -> None:
            target.write_bytes(b"partial")
            raise RuntimeError("compile failed")

        with self.assertRaises(RuntimeError):
            self.store.produce("pre", H, broken)
        self.assertIsNone(self.store.get("pre", H))
        leftovers = [p.name for p in (self.root / "cache").rglob("*") if p.is_file() and p.name != ".lock"]
        self.assertEqual(leftovers, [])
        path = self.store.produce("pre", H, lambda target: target.write_bytes(b"ok"))
        self.assertEqual(path.read_bytes(), b"ok")

    def test_two_threads_on_one_key_make_once(self) -> None:
        # Holds whichever thread wins: the loser either waits on the winner or finds the file.
        made: list[int] = []
        second_started = threading.Event()
        results: list[Path] = []

        def make(target: Path) -> None:
            made.append(1)
            second_started.wait()
            target.write_bytes(b"once")

        def run(announce: bool) -> None:
            if announce:
                second_started.set()
            results.append(self.store.produce("pre", H, make))

        threads = [threading.Thread(target=run, args=(False,)), threading.Thread(target=run, args=(True,))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(made), 1)
        self.assertEqual(results[0], results[1])
        self.assertEqual(results[0].read_bytes(), b"once")

    def test_in_process_memo_reuses_by_content_without_shared_mutation(self) -> None:
        import copy

        made = []

        def compute():
            made.append(1)
            return {"source": "real text"}

        first = cache.memo("test-kind", ("same", 1), compute, size=cache.memory_size, copy_out=copy.deepcopy)
        first["source"] = "mutated"
        second = cache.memo("test-kind", ("same", 1), compute, size=cache.memory_size, copy_out=copy.deepcopy)
        self.assertEqual(second["source"], "real text")
        self.assertEqual(len(made), 1)


class TrimTests(TempCase):
    def fill(self, rows: list[tuple[str, int, int, int]]) -> list[Path]:
        """rows: (key prefix, size, atime, mtime); returns the entry paths in row order."""
        store = cache.Cache(self.root)
        paths = []
        for prefix, size, atime, mtime in rows:
            key = (prefix * 64)[:64]
            path = store.put("obj", key, write(self.root / f"src-{prefix}", size))
            os.utime(path, ns=(atime, mtime))
            paths.append(path)
        return paths

    def test_removes_least_recently_used_down_to_target(self) -> None:
        cases = [
            ("oldest atime first", [("a", 100, 1, 9), ("b", 100, 2, 1), ("c", 100, 3, 1)], 250, 150, ["a", "b"]),
            ("mtime breaks atime ties", [("a", 100, 5, 7), ("b", 100, 5, 3), ("c", 100, 5, 9)], 250, 150, ["b", "a"]),
            ("name breaks full ties", [("c", 100, 5, 5), ("a", 100, 5, 5), ("b", 100, 5, 5)], 250, 150, ["a", "b"]),
            ("under the cap removes nothing", [("a", 100, 1, 1), ("b", 100, 2, 2)], 250, 100, []),
            ("exactly at the cap removes nothing", [("a", 100, 1, 1), ("b", 100, 2, 2)], 200, 100, []),
        ]
        for label, rows, max_bytes, to_bytes, expected in cases:
            with self.subTest(label):
                paths = self.fill_fresh(rows)
                removed = cache.trim(self.root, max_bytes, to_bytes)
                self.assertEqual([p.name[0] for p in removed], expected)
                for path in paths:
                    self.assertEqual(path.exists(), path.name[0] not in expected)

    def fill_fresh(self, rows: list[tuple[str, int, int, int]]) -> list[Path]:
        for child in self.root.iterdir():
            if child.is_dir():
                for path in child.rglob("*"):
                    path.unlink() if path.is_file() else None
        return self.fill(rows)

    def test_lock_files_are_never_deleted_or_counted(self) -> None:
        paths = self.fill([("a", 100, 1, 1), ("b", 100, 2, 2)])
        locks = [path.parent / ".lock" for path in paths]
        for lock in locks:
            lock.write_bytes(b"y" * 10_000)
        removed = cache.trim(self.root, 150, 50)
        self.assertEqual(len(removed), 2)
        self.assertTrue(all(lock.exists() for lock in locks))
