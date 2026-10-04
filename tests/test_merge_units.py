"""Merge-unit runs: maximal runs of adjacent landed members within one group."""

import unittest

from unbake.layout import merge_units
from unbake.layout.map import Group


def group(name: str, members: str, split: str = "") -> Group:
    return Group(name, "main", "default", tuple(members), split=tuple(split))


class RunsTests(unittest.TestCase):
    def test_runs(self) -> None:
        g = group("g", "abcde")
        cases = [
            ("all landed is one run", [g], set("abcde"), [("a", "b", "c", "d", "e")]),
            ("unlanded member breaks the run", [g], set("abde"), [("a", "b"), ("d", "e")]),
            ("single landed members are no run", [g], set("ace"), []),
            ("nothing landed", [g], set(), []),
            ("runs never cross groups", [group("g", "ab"), group("h", "cd")], set("bc"), []),
            ("two groups each give a run", [group("g", "ab"), group("h", "cd")], set("abcd"), [("a", "b"), ("c", "d")]),
            (
                "a split member starts a new run",
                [group("g", "abcde", "c")],
                set("abcde"),
                [("a", "b"), ("c", "d", "e")],
            ),
            ("split leaving one member drops it", [group("g", "abc", "c")], set("abc"), [("a", "b")]),
            ("unknown landed names are ignored", [group("g", "ab")], {"a", "b", "zzz"}, [("a", "b")]),
        ]
        for label, groups, landed, expected in cases:
            with self.subTest(label):
                found = merge_units.member_runs(groups, landed, lambda left, right: True)
                self.assertEqual([members for _, members in found], expected)

    def test_rows_that_do_not_join_break_the_run(self) -> None:
        found = merge_units.member_runs([group("g", "abc")], set("abc"), lambda left, right: left != "b")
        self.assertEqual([members for _, members in found], [("a", "b")])
