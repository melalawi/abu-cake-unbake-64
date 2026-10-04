"""Resident constant blocks are deleted from published sources."""

import unittest
from types import SimpleNamespace
from unittest import mock

from tests.kit import TempCase
from unbake.config import Held
from unbake.layout import resident

M = resident.MARKER


def text(*lines: str) -> str:
    return "\n".join(["int f(void) { return 0; }", *lines, "int g;", ""])


class DeletedTests(unittest.TestCase):
    def test_blocks(self) -> None:
        cases = [
            (
                "per-version branches",
                ["", M, "#if defined(VERSION_US)", "const float unbake_rodata_80001000_4 = 0.5f;",
                 "#elif defined(VERSION_DE) || defined(VERSION_EU)",
                 "const unsigned char unbake_rodata_80002000_3[] = {0x25, 0x73, 0x00};", "#endif"],
                [],
            ),
            ("unconditional definitions", [M, "const double unbake_rodata_u_0 = 2.0;"], []),
            ("empty first branch", [M, "#if defined(VERSION_DE)", "#else", "const float unbake_rodata_u_0 = 1.0f;",
                                    "#endif"], []),
            (
                "two blocks around code",
                ["", M, "const float unbake_rodata_u_0 = 1.0f;", "", "void h(void) {}", "", M,
                 "#if defined(VERSION_US)", "const unsigned int unbake_rodata_u_1[] = {0x00201650U};", "#endif"],
                ["", "void h(void) {}"],
            ),
            ("no marker", ["#if defined(VERSION_US)", "int x;", "#endif"],
             ["#if defined(VERSION_US)", "int x;", "#endif"]),
        ]  # fmt: skip
        for label, before, after in cases:
            with self.subTest(label):
                self.assertEqual(resident.deleted(text(*before), "u"), text(*after))

    def test_refusals_name_the_unit(self) -> None:
        cases = [
            ("stray line", [M, "#if defined(VERSION_US)", "int x;", "#endif"], "expected #endif, found 'int x;'"),
            ("missing endif", [M, "#if defined(VERSION_US)", "const float unbake_rodata_u_0 = 1.0f;"], "the end of"),
            (
                "a deleted name is still used",
                [M, "const float unbake_rodata_u_0 = 1.0f;", "float h(void) { return unbake_rodata_u_0; }"],
                "source still uses unbake_rodata_u_0",
            ),
        ]
        for label, lines, message in cases:
            with self.subTest(label):
                with self.assertRaises(Held) as raised:
                    resident.deleted("\n".join(lines), "u")
                self.assertEqual(raised.exception.phase, "resident")
                self.assertIn("resident.u: ", str(raised.exception))
                self.assertIn(message, str(raised.exception))


class RunTests(TempCase):
    def test_rewrites_only_sources_with_blocks_and_commits_them_once(self) -> None:
        src = self.root / "src"
        src.mkdir()
        (src / "a.c").write_text(text("", M, "const float unbake_rodata_a_0 = 0.5f;"))
        (src / "b.c").write_text("int b;\n")
        project = SimpleNamespace(src=src)
        with mock.patch("unbake.land._commit") as commit:
            changed = resident.run(project, SimpleNamespace())
        self.assertEqual(changed, [src / "a.c"])
        self.assertEqual((src / "a.c").read_text(), text())
        commit.assert_called_once_with(
            project, mock.ANY, [src / "a.c"], "Delete resident constant blocks the link discards"
        )
        with mock.patch("unbake.land._commit") as commit:
            self.assertEqual(resident.run(project, SimpleNamespace()), [])
        commit.assert_not_called()
