"""Shared alias maps are read once per process: a cycle's later solves reuse what an earlier one read."""

from unittest.mock import patch

from tests.kit import TempCase
from unbake import cache
from unbake.cache import Cache
from unbake.config import Held
from unbake.typemap import facts


class SharedValueTests(TempCase):
    def setUp(self) -> None:
        super().setUp()
        cache.forget(["facts.shared"])
        self.addCleanup(cache.forget, ["facts.shared"])

    def test_later_solves_reuse_a_shared_value_and_a_missing_one_is_always_refused(self) -> None:
        aliases = {"s32": "int"}
        encoded = facts.Store(Cache(self.root / "cache")).encode({"aliases": aliases})
        reads: list[str] = []
        real = facts._load

        def counted(path):  # type: ignore[no-untyped-def]
            reads.append(path.name)
            return real(path)

        with patch.object(facts, "_load", counted):
            first = facts.Store(Cache(self.root / "cache")).decode(encoded)
            second = facts.Store(Cache(self.root / "cache")).decode(encoded)
            self.assertEqual(first["aliases"], aliases)
            self.assertIs(first["aliases"], second["aliases"])
            self.assertEqual(len(reads), 1)
            missing = {"aliases": {"$shared": "0" * 64}}
            for _ in range(2):  # a refusal is never remembered as a value
                with self.assertRaisesRegex(Held, "facts.shared: missing " + "0" * 64):
                    facts.Store(Cache(self.root / "cache")).decode(missing)


class SolveChangesTests(TempCase):
    def test_changes_name_what_moved_and_nothing_else(self) -> None:
        from unbake.typemap import solver

        before = {"globals": {"a": {"semantic_sha256": "1"}, "b": {"semantic_sha256": "2"}}, "functions": {}}
        after = {
            "globals": {"a": {"semantic_sha256": "1"}, "b": {"semantic_sha256": "3"}, "c": {"semantic_sha256": "4"}}
        }
        found = solver.changes(before, after, shown=1)
        same = solver.changes(after, after)
        self.assertEqual(found, {"globals": {"count": 2, "first": ["b"]}})
        self.assertEqual(same, {})
