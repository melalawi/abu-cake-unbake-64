"""Merge-unit runs: maximal runs of adjacent matched members within one group."""

import unittest

from unbake.layout import merge_units


def layout(*groups: tuple[str, list[str]], splits: list[tuple[str, str]] | None = None) -> dict:
    return {
        "group": [{"name": name, "segment": "main", "members": members} for name, members in groups],
        "split": [list(pair) for pair in splits or []],
    }


# DRAFT interface: the layout argument is the parsed layout.toml table; splits are a top-level list of pairs.
class RunsTests(unittest.TestCase):
    def test_runs(self) -> None:
        abcde = ["a", "b", "c", "d", "e"]
        cases = [
            ("all matched is one run", layout(("g", abcde)), set(abcde), [("a", "b", "c", "d", "e")]),
            ("unmatched member breaks the run", layout(("g", abcde)), {"a", "b", "d", "e"}, [("a", "b"), ("d", "e")]),
            ("single matched member is no run", layout(("g", abcde)), {"a", "c", "e"}, []),
            ("nothing landed", layout(("g", abcde)), set(), []),
            ("runs never cross groups", layout(("g", ["a", "b"]), ("h", ["c", "d"])), {"b", "c"}, []),
            ("two groups each give a run", layout(("g", ["a", "b"]), ("h", ["c", "d"])), set("abcd"), [("a", "b"), ("c", "d")]),
            ("split pair is excluded", layout(("g", abcde), splits=[("b", "c")]), set(abcde), [("a", "b"), ("c", "d", "e")]),
            ("split leaving one member drops it", layout(("g", ["a", "b", "c"]), splits=[("b", "c")]), set("abc"), [("a", "b")]),
            ("unlanded names are ignored", layout(("g", ["a", "b"])), {"a", "b", "zzz"}, [("a", "b")]),
        ]
        for label, table, landed, expected in cases:
            with self.subTest(label):
                self.assertEqual(sorted(merge_units.runs(table, landed)), sorted(expected))
