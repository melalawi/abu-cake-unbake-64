"""Module inference: same-object joins, agreed padding cuts and the cap as the last resort."""

import unittest

from unbake.layout.map import Member
from unbake.layout.modules import Evidence, Function, padding, plan, version_evidence

RETURN, NOP = 0x03E00008, 0


def lui(register: int, high: int) -> int:
    return 0x3C000000 | register << 16 | high


def lw(register: int, base: int, low: int) -> int:
    return 0x8C000000 | base << 21 | register << 16 | low


def jal(address: int) -> int:
    return 0x0C000000 | (address & 0x0FFFFFFF) >> 2


def load(address: int) -> list[int]:
    return [lui(8, address >> 16), lw(9, 8, address & 0xFFFF)]


class PaddingTests(unittest.TestCase):
    def test_padding_counts_zero_words_after_the_delay_slot(self) -> None:
        for name, words, expected in (
            ("delay slot nop only", [1, RETURN, NOP], 0),
            ("two pad words", [1, RETURN, NOP, NOP, NOP], 2),
            ("filled delay slot then pad", [1, RETURN, 5, NOP], 1),
            ("no return before zeros", [1, 2, NOP], 1),
            ("all zero", [NOP, NOP], 0),
        ):
            with self.subTest(name):
                self.assertEqual(padding(words), expected)


class VersionEvidenceTests(unittest.TestCase):
    CONSTANTS = range(0x80100000, 0x80101000)

    def evidence(self, bodies: dict[str, list[int]], segments: dict[str, str] | None = None, cap: int = 8) -> Evidence:
        functions, code, start = [], {}, 0x1000
        for name, words in bodies.items():
            functions.append(
                Function(name, (segments or {}).get(name, "main"), start, start + 4 * len(words), 0x80000000 + start)
            )
            code[name] = words
            start += 4 * len(words)
        return version_evidence(functions, lambda f: code[f.name], self.CONSTANTS.__contains__, None, cap)

    def test_joins_and_cuts(self) -> None:
        shared = load(0x80100010)
        cases = (
            (
                "shared constant joins its loaders",
                {"a": [*shared, RETURN, NOP], "b": [RETURN, NOP], "c": [*shared, RETURN, NOP]},
                None,
                8,
                [("a", "c", "rodata")],
                set(),
            ),
            (
                "a constant loaded once joins nothing",
                {"a": [*shared, RETURN, NOP], "b": [RETURN, NOP]},
                None,
                8,
                [],
                set(),
            ),
            (
                "loads outside constant ranges join nothing",
                {"a": [*load(0x80200000), RETURN, NOP], "b": [*load(0x80200000), RETURN, NOP]},
                None,
                8,
                [],
                set(),
            ),
            (
                "evidence never reaches past the cap",
                {"a": [*shared, RETURN, NOP], "b": [RETURN, NOP], "c": [*shared, RETURN, NOP]},
                None,
                2,
                [],
                set(),
            ),
            (
                "evidence never crosses segments",
                {"a": [*shared, RETURN, NOP], "c": [*shared, RETURN, NOP]},
                {"c": "other"},
                8,
                [],
                set(),
            ),
            (
                "a static callee joins its caller window",
                {"a": [jal(0x80001010), NOP, RETURN, NOP], "b": [RETURN, NOP]},
                None,
                8,
                [("a", "b", "callee")],
                set(),
            ),
            (
                "padding before an aligned start is a cut",
                {"a": [1, RETURN, NOP, NOP], "b": [RETURN, NOP]},
                None,
                8,
                [],
                {("a", "b")},
            ),
            ("an unaligned start is never a cut", {"a": [1, RETURN, NOP], "b": [RETURN, NOP]}, None, 8, [], set()),
        )
        for name, bodies, segments, cap, joins, cuts in cases:
            with self.subTest(name):
                found = self.evidence(bodies, segments, cap)
                self.assertEqual((found.joins, found.cuts), (joins, cuts))


def members(names: str, partial: str = "", versions: tuple[str, ...] = ("us", "eu")) -> list[Member]:
    return [
        Member(name, "main", index * 0x10, ("us",) if name in partial else versions) for index, name in enumerate(names)
    ]


def orders(names: str, versions: tuple[str, ...] = ("us", "eu")) -> dict[str, dict[str, int]]:
    return {version: {name: index for index, name in enumerate(names)} for version in versions}


class PlanTests(unittest.TestCase):
    def test_partition(self) -> None:
        none = {"us": Evidence(), "eu": Evidence()}
        cut_bc = {"us": Evidence(cuts={("b", "c")}), "eu": Evidence(cuts={("b", "c")})}
        cases = (
            (
                "no evidence packs to the cap",
                "abcde",
                "",
                none,
                2,
                set(),
                [("ab", ["cap"]), ("cd", ["cap"]), ("e", [])],
            ),
            (
                "a join keeps its run whole and the cap closes before it",
                "abcde",
                "",
                {"us": Evidence(joins=[("b", "d", "rodata")]), "eu": Evidence()},
                3,
                set(),
                [("a", ["cap"]), ("bcd", ["cap", "rodata"]), ("e", [])],
            ),
            (
                "a joined run longer than the cap stays whole",
                "abcde",
                "",
                {"us": Evidence(joins=[("a", "e", "callee")]), "eu": Evidence()},
                2,
                set(),
                [("abcde", ["callee"])],
            ),
            ("an agreed padding cut splits", "abcd", "", cut_bc, 8, set(), [("ab", ["padding"]), ("cd", [])]),
            (
                "a cut one version does not show is no cut",
                "abcd",
                "",
                {"us": Evidence(cuts={("b", "c")}), "eu": Evidence()},
                8,
                set(),
                [("abcd", [])],
            ),
            (
                "a join never crosses a cut",
                "abcd",
                "",
                {"us": Evidence(joins=[("a", "d", "callee")], cuts={("b", "c")}), "eu": Evidence(cuts={("b", "c")})},
                8,
                set(),
                [("ab", ["padding"]), ("cd", [])],
            ),
            (
                "a version-only member stays with its predecessor",
                "abcd",
                "c",
                none,
                3,
                set(),
                [("abc", ["cap", "version"]), ("d", [])],
            ),
            (
                "version joins yield to the cap",
                "abcde",
                "bcde",
                none,
                2,
                set(),
                [("ab", ["cap", "version"]), ("cd", ["cap"]), ("e", [])],
            ),
            ("a recorded split starts a module", "abcd", "", none, 8, {"c"}, [("ab", ["split"]), ("cd", [])]),
        )
        for name, names, partial, evidence, cap, cuts, expected in cases:
            with self.subTest(name):
                found = plan(members(names, partial), ("us", "eu"), evidence, orders(names), cap, cuts)
                self.assertEqual([("".join(group), list(signals)) for group, signals in found], expected)

    def test_a_cut_needs_adjacent_rows_in_every_holding_version(self) -> None:
        evidence = {"us": Evidence(cuts={("a", "b")}), "eu": Evidence(cuts={("a", "b")})}
        order = {"us": {"a": 0, "b": 1}, "eu": {"a": 0, "x": 1, "b": 2}}
        found = plan(members("ab"), ("us", "eu"), evidence, order, 8)
        self.assertEqual([group for group, _ in found], [("a", "b")])
