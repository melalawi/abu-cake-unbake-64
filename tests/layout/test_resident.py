"""Resident constant blocks: version-neutral names, one branch per identical block."""

import unittest
from types import SimpleNamespace
from unittest import mock

from tests.kit import TempCase
from unbake.config import Held
from unbake.layout import resident

MACROS = {"de": ("VERSION_DE",), "eu": ("VERSION_EU",), "us": ("VERSION_US",)}
M = resident.MARKER


def text(*lines: str) -> str:
    return "\n".join(["int f(void) { return 0; }", *lines, "int g;", ""])


class CanonicalTests(unittest.TestCase):
    def test_blocks(self) -> None:
        cases = [
            (
                "identical in every version is unconditional",
                [M, "#if defined(VERSION_DE)", "const float unbake_rodata_80001000_4 = 0.5f;",
                 "#elif defined(VERSION_EU) || defined(VERSION_US)", "const float unbake_rodata_80002000_4 = 0.5f;",
                 "#endif"],
                [M, "const float unbake_rodata_u_0 = 0.5f;"],
            ),
            (
                "versions group by equal blocks in version order",
                [M, "#if defined(VERSION_US)", "const float unbake_rodata_80001000_4 = 0.5f;",
                 "const double unbake_rodata_80001008_8 = 2.0;",
                 "#elif defined(VERSION_DE)", "const float unbake_rodata_80003000_4 = 1.0f;",
                 "#elif defined(VERSION_EU)", "const float unbake_rodata_80002000_4 = 0.5f;",
                 "const double unbake_rodata_80002008_8 = 2.0;", "#endif"],
                [M, "#if defined(VERSION_DE)", "const float unbake_rodata_u_0 = 1.0f;",
                 "#elif defined(VERSION_EU) || defined(VERSION_US)", "const float unbake_rodata_u_0 = 0.5f;",
                 "const double unbake_rodata_u_1 = 2.0;", "#endif"],
            ),
            (
                "a version without the block gets no branch",
                [M, "#if defined(VERSION_EU)", "const unsigned char unbake_rodata_80002000_3[] = {0x25, 0x73, 0x00};",
                 "#elif defined(VERSION_US)", "const unsigned char unbake_rodata_80001000_3[] = {0x25, 0x73, 0x00};",
                 "#endif"],
                [M, "#if defined(VERSION_EU) || defined(VERSION_US)",
                 "const unsigned char unbake_rodata_u_0[] = {0x25, 0x73, 0x00};", "#endif"],
            ),
            (
                "a later block starts after the longest earlier branch",
                [M, "#if defined(VERSION_DE)", "const float unbake_rodata_80003000_4 = 1.0f;",
                 "#elif defined(VERSION_EU) || defined(VERSION_US)", "const float unbake_rodata_80001000_4 = 0.5f;",
                 "const float unbake_rodata_80001004_4 = 0.25f;", "#endif",
                 "void h(void) {}",
                 M, "const unsigned int unbake_rodata_u_7[] = {0x00201650U, 0x002015B4U};"],
                [M, "#if defined(VERSION_DE)", "const float unbake_rodata_u_0 = 1.0f;",
                 "#elif defined(VERSION_EU) || defined(VERSION_US)", "const float unbake_rodata_u_0 = 0.5f;",
                 "const float unbake_rodata_u_1 = 0.25f;", "#endif",
                 "void h(void) {}",
                 M, "const unsigned int unbake_rodata_u_2[] = {0x00201650U, 0x002015B4U};"],
            ),
            ("a block with no definitions is removed", [M, "#if defined(VERSION_DE)", "#endif"], []),
        ]  # fmt: skip
        for label, before, after in cases:
            with self.subTest(label):
                result = resident.canonical(text(*before), "u", MACROS)
                self.assertEqual(result, text(*after))
                self.assertEqual(resident.canonical(result, "u", MACROS), result)

    def test_source_without_marker_is_unchanged(self) -> None:
        source = text("#if defined(VERSION_US)", "const float unbake_rodata_80001000_4 = 0.5f;", "#endif")
        self.assertEqual(resident.canonical(source, "u", MACROS), source)

    def test_refusals_name_the_unit(self) -> None:
        cases = [
            ("unknown macro", [M, "#if defined(VERSION_JP)", "#endif"], "VERSION_JP is no version's macro"),
            (
                "version in two branches",
                [M, "#if defined(VERSION_US)", "#elif defined(VERSION_US)", "#endif"],
                "version us has two branches",
            ),
            ("stray line", [M, "#if defined(VERSION_US)", "int x;", "#endif"], "unexpected 'int x;'"),
            ("elif first", [M, "#elif defined(VERSION_US)", "#endif"], "unexpected '#elif defined(VERSION_US)'"),
            ("missing endif", [M, "#if defined(VERSION_US)", "const float unbake_rodata_u_0 = 1.0f;"], "no #endif"),
        ]
        for label, lines, message in cases:
            with self.subTest(label):
                with self.assertRaises(Held) as raised:
                    resident.canonical("\n".join(lines), "u", MACROS)
                self.assertEqual(raised.exception.phase, "resident")
                self.assertIn("resident.u: ", str(raised.exception))
                self.assertIn(message, str(raised.exception))


class RunTests(TempCase):
    def test_rewrites_only_noncanonical_sources_and_commits_them_once(self) -> None:
        src = self.root / "src"
        src.mkdir()
        stale = text(M, "#if defined(VERSION_DE) || defined(VERSION_EU) || defined(VERSION_US)",
                     "const float unbake_rodata_80001000_4 = 0.5f;", "#endif")  # fmt: skip
        (src / "a.c").write_text(stale)
        (src / "b.c").write_text(text(M, "const float unbake_rodata_b_0 = 0.5f;"))
        (src / "c.c").write_text("int c;\n")
        project = SimpleNamespace(
            src=src, versions=tuple(MACROS), version=lambda version: SimpleNamespace(macros=MACROS[version])
        )
        with mock.patch("unbake.land._commit") as commit:
            changed = resident.run(project, SimpleNamespace())
        self.assertEqual(changed, [src / "a.c"])
        self.assertEqual((src / "a.c").read_text(), text(M, "const float unbake_rodata_a_0 = 0.5f;"))
        commit.assert_called_once_with(project, mock.ANY, [src / "a.c"], "Name resident constants without addresses")
        with mock.patch("unbake.land._commit") as commit:
            self.assertEqual(resident.run(project, SimpleNamespace()), [])
        commit.assert_not_called()
