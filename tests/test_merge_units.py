"""Merge-unit runs: maximal runs of adjacent landed members within one group."""

import unittest
from unittest import mock

from tests.project_fixture import ProjectCase
from unbake import config, land
from unbake.layout import map as layout_map
from unbake.layout import merge_units
from unbake.layout.map import Group


def in_process(host, fn, items):
    return [fn(item) for item in items]


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


class RunTests(ProjectCase):
    versions = ("us",)

    def test_a_proven_run_drops_absorbed_members_and_the_layout_still_loads(self) -> None:
        layout = self.project.root / "layout.toml"
        groups = (Group("g", "main", "default", ("alpha", "beta")), Group("gamma", "main", "default", ("gamma",)))
        layout.write_bytes(layout_map.encoded(layout_map.Map(2, groups)))
        split_path = self.project.version("us").split
        for name in ("alpha", "beta"):
            (self.project.src / f"{name}.c").write_text(f"int {name}(void) {{ return 0; }}\n")
            split_path.write_text(split_path.read_text().replace(f"asm, {name}]", f"c, {name}]"))
        project = config.load(self.project.root)
        with (
            mock.patch.object(merge_units, "prove", return_value=True),
            mock.patch.object(land, "_commit") as commit,
            mock.patch("unbake.buildfiles.write", return_value=[]),
            mock.patch("unbake.pool.run", side_effect=in_process),
        ):
            lines = merge_units.run(project, self.host)
        self.assertEqual(lines, ["merge g alpha..beta: proven"])
        commit.assert_called_once()
        loaded = {group.name: group for group in layout_map.load(config.load(self.project.root)).groups}
        self.assertEqual((loaded["g"].members, loaded["g"].evidence), (("alpha",), "proven"))
        self.assertNotIn("beta]", split_path.read_text())
        self.assertFalse((self.project.src / "beta.c").exists())


class SecondRunTests(ProjectCase):
    versions = ("us",)

    def test_a_later_run_in_the_same_group_keeps_earlier_merges(self) -> None:
        layout = self.project.root / "layout.toml"
        g = Group("g", "main", "default", ("alpha", "beta", "gamma"))
        layout.write_bytes(layout_map.encoded(layout_map.Map(2, (g,))))
        split_path = self.project.version("us").split
        for name in ("alpha", "beta", "gamma"):
            (self.project.src / f"{name}.c").write_text(f"int {name}(void) {{ return 0; }}\n")
            split_path.write_text(split_path.read_text().replace(f"asm, {name}]", f"c, {name}]"))
        project = config.load(self.project.root)
        with (
            mock.patch.object(merge_units, "runs", return_value=[(g, ("alpha", "beta")), (g, ("gamma",))]),
            mock.patch.object(
                merge_units, "prove", side_effect=lambda project, host, members, source: len(members) > 1
            ),
            mock.patch.object(land, "_commit") as commit,
            mock.patch("unbake.buildfiles.write", return_value=[]),
            mock.patch("unbake.pool.run", side_effect=in_process),
        ):
            lines = merge_units.run(project, self.host)
        loaded = layout_map.load(config.load(self.project.root)).groups
        self.assertEqual(loaded[0].members, ("alpha", "gamma"))
        self.assertEqual(lines[1], "merge g gamma..gamma: refused; recorded as split")
        # Proofs run in the pool; the single writer commits the whole pass once.
        commit.assert_called_once()


class VersionOnlyRunTests(ProjectCase):
    versions = ("us", "eu")

    def test_absorbed_version_only_members_take_their_marks_and_a_later_run_still_loads(self) -> None:
        # RW hold: run 228 absorbed members with `only` marks; run 229 then refused `layout.only.<absorbed>`.
        layout = self.project.root / "layout.toml"
        marks = {"alpha": ("us",), "beta": ("us",)}
        g = Group("g", "main", "default", ("alpha", "beta", "gamma"), only=marks, split=("beta",))
        layout.write_bytes(layout_map.encoded(layout_map.Map(2, (g,))))
        split_path = self.project.version("eu").split
        split_path.write_text(
            "".join(x for x in split_path.read_text().splitlines(True) if "alpha]" not in x and "beta]" not in x)
        )
        us = self.project.version("us").split
        for name in ("alpha", "beta"):
            (self.project.src / f"{name}.c").write_text(f"int {name}(void) {{ return 0; }}\n")
            us.write_text(us.read_text().replace(f"asm, {name}]", f"c, {name}]"))
        (self.project.src / "gamma.c").write_text("int gamma(void) { return 0; }\n")
        project = config.load(self.project.root)
        with (
            mock.patch.object(merge_units, "runs", return_value=[(g, ("alpha", "beta")), (g, ("gamma",))]),
            mock.patch.object(merge_units, "prove", return_value=True),
            mock.patch.object(land, "_commit"),
            mock.patch("unbake.buildfiles.write", return_value=[]),
            mock.patch("unbake.pool.run", side_effect=in_process),
        ):
            merge_units.run(project, self.host)
        loaded = layout_map.load(config.load(self.project.root)).groups[0]
        self.assertEqual((loaded.members, loaded.only, loaded.split), (("alpha", "gamma"), {"alpha": ("us",)}, ()))
