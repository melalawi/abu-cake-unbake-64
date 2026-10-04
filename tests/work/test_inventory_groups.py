"""Inventory grouping: shared names join, equal bytes join only rows of distinct versions."""

import unittest
from typing import ClassVar

from unbake.layout.split import Function
from unbake.work.inventory import groups


def fn(version: str, name: str, *aliases: str) -> Function:
    return Function(version, name, 0, 4, 0, name, "asm", aliases)


def grouped(rows: list[tuple[Function, bytes]]) -> list[list[tuple[str, str]]]:
    bodies = {(item.version, item.name): body for item, body in rows}
    result = groups([item for item, _ in rows], bodies)
    return sorted(sorted((item.version, item.name) for item in group) for group in result)


class GroupTests(unittest.TestCase):
    CASES: ClassVar[dict[str, tuple[list[tuple[Function, bytes]], list[list[tuple[str, str]]]]]] = {
        "same name joins": (
            [(fn("us", "f"), b"a"), (fn("de", "f"), b"b")],
            [[("de", "f"), ("us", "f")]],
        ),
        "alias joins": (
            [(fn("us", "f", "g"), b"a"), (fn("de", "g"), b"b")],
            [[("de", "g"), ("us", "f")]],
        ),
        "equal bytes join distinct versions": (
            [(fn("us", "f"), b"a"), (fn("de", "g"), b"a")],
            [[("de", "g"), ("us", "f")]],
        ),
        "equal bytes within one version stay apart": (
            [(fn("us", "f"), b"a"), (fn("us", "g"), b"a"), (fn("de", "h"), b"a")],
            [[("de", "h")], [("us", "f")], [("us", "g")]],
        ),
        "equal bytes never join groups that share a version": (
            [(fn("us", "f"), b"a"), (fn("de", "f"), b"x"), (fn("de", "g"), b"a")],
            [[("de", "f"), ("us", "f")], [("de", "g")]],
        ),
        "a joined group carries its versions": (
            [(fn("us", "f"), b"a"), (fn("de", "g"), b"a"), (fn("eu", "h"), b"a"), (fn("eu", "g"), b"z")],
            [[("de", "g"), ("eu", "g"), ("us", "f")], [("eu", "h")]],
        ),
    }

    def test_cases(self) -> None:
        for label, (rows, expected) in self.CASES.items():
            with self.subTest(label):
                self.assertEqual(grouped(rows), expected)


if __name__ == "__main__":
    unittest.main()
