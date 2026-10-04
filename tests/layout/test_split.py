"""Split edits against small layouts and a fake compare/build boundary."""

import dataclasses
import tempfile
import unittest
from collections.abc import Sequence
from pathlib import Path
from types import SimpleNamespace

from unbake.config import Held
from unbake.layout import split, split_analysis, split_apply, split_create, split_edits


class ProjectFixture:
    def __init__(self, root: Path, versions: tuple[str, ...] = ("us", "eu")) -> None:
        self.root = root
        self.name = "fixture"
        self.build = root / "build"
        self.roms = root / "roms"
        self.roms.mkdir()
        self.versions = versions
        self.src = root / "src"
        self.include = (root / "include",)
        self.version_map = {}
        for index, v in enumerate(versions):
            directory = root / "versions" / v
            directory.mkdir(parents=True)
            rom = self.roms / f"baserom.{v}.z64"
            rom.write_bytes(bytes(range(128)))
            self.version_map[v] = SimpleNamespace(
                name=v, split=directory / "fixture.yaml", symbols=directory / "symbol_addrs.txt", baserom=rom
            )
            self.layout(
                v,
                [(0x10, "asm", "alpha"), (0x20, "asm", "beta"), (0x38, "asm", "gamma"), (0x40, "data", "pool")],
                vram=0x80001000 + index * 0x100,
            )
        self.policy = SimpleNamespace(assignment_idle_hours=3, state_root=root / "state")

    def version(self, v: str) -> SimpleNamespace:
        if v not in self.version_map:
            raise Held("config", f"version {v}")
        return self.version_map[v]

    def build_link(self, v: str) -> Path:
        return self.root / "build" / v

    def layout(
        self, v: str, rows: Sequence[tuple[int, str, str]], *, vram: int = 0x80001000, newline: str = "\n"
    ) -> None:
        version = self.version(v)
        text = (
            "name: fixture\noptions:\n  basename: fixture\n\nsegments:\n"
            "  - [0x000000, header, header]\n"
            "  - name: code\n    type: code\n    start: 0x10\n"
            f"    vram: 0x{vram:08X}\n    subsegments:\n"
        )
        text += "".join(f"      - [0x{start:06X}, {kind}, {name}]\n" for start, kind, name in rows)
        text += "  - [0x000060]\n"
        version.split.write_bytes(text.replace("\n", newline).encode())
        symbols = "".join(f"{Path(name).name} = 0x{vram + start - 0x10:08X};\n" for start, kind, name in rows)
        version.symbols.write_bytes(symbols.replace("\n", newline).encode())

    def generations(self) -> None:
        directory = self.root / "build"
        directory.mkdir()
        for v in self.versions:
            generation = directory / f"{v}.0"
            generation.mkdir()
            (generation / "untouched").write_text(v)
            self.build_link(v).symlink_to(generation.name)


class SplitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = ProjectFixture(Path(self.temp.name))

    def test_cut_preserves_other_lines_and_places_vram_symbol(self) -> None:
        version = self.project.version("us")
        before = split.read(version.split)
        edits = split_edits.cut(self.project, "us", "tiny", 0x14, 0x1C)
        self.assertEqual(split.read(version.split), before)
        yaml = next(edit for edit in edits if edit.path == version.split)
        self.assertEqual(
            yaml.after,
            before.replace(
                "      - [0x000010, asm, alpha]\n",
                "      - [0x000010, asm, alpha]\n"
                "      - [0x000014, asm, tiny]\n"
                "      - [0x00001C, asm, alpha_00001C]\n",
            ),
        )
        symbols = next(edit for edit in edits if edit.path == version.symbols)
        self.assertTrue(symbols.after.endswith("tiny = 0x80001004;\n"))
        self.assertIn("+      - [0x000014, asm, tiny]", split_apply.diff(edits))
        self.assertIn("@@", split_apply.diff(edits))

    def test_cut_at_start_gives_suffix_a_distinct_output_path(self) -> None:
        edits = split_edits.cut(self.project, "us", "entry", 0x10, 0x18)
        edit = edits[0]
        self.assertIn("[0x000010, asm, entry]", edit.after)
        self.assertIn("[0x000018, asm, alpha_000018]", edit.after)
        for item in edits:
            item.path.write_text(item.after)
        inventory = split.functions(self.project, "us")
        self.assertEqual(inventory[0].name, "entry")
        self.assertEqual(inventory[1].name, "alpha_000018")
        self.assertNotIn("alpha", inventory[1].aliases)

    def test_data_cut_and_crlf_and_quoted_path(self) -> None:
        self.project.layout("us", [(0x10, "asm", "folder/alpha"), (0x40, "data", "pool")], newline="\r\n")
        path = self.project.version("us").split
        path.write_bytes(path.read_bytes().replace(b"folder/alpha", b'"folder/alpha"'))
        edit = split_edits.data_cut(self.project, "us", "literal", 0x20, 0x28)[0]
        self.assertIn('[0x000020, data, "folder/literal"]\r\n', edit.after)
        self.assertNotIn("\n", edit.after.replace("\r\n", ""))
        self.assertEqual(edit.after.splitlines().count("name: fixture"), 1)

    def test_invalid_cut_ranges_and_missing_values_are_held(self) -> None:
        for start, end in ((0x14, 0x24), (0x15, 0x1C), (0x18, 0x14), (0x40, 0x48), (0x70, 0x74)):
            with self.subTest(start=start), self.assertRaises(Held):
                split_edits.cut(self.project, "us", "tiny", start, end)
        for field, value in (("function", None), ("start", None), ("end", None)):
            arguments = dict(function="tiny", start=0x14, end=0x1C)
            arguments[field] = value
            with self.subTest(field=field), self.assertRaisesRegex(Held, field):
                split_edits.cut(self.project, "us", **arguments)

    def test_cut_requires_explicit_segment_vram(self) -> None:
        path = self.project.version("us").split
        path.write_text(split.read(path).replace("    vram: 0x80001000\n", ""))
        with self.assertRaisesRegex(Held, "vram"):
            split_edits.cut(self.project, "us", "tiny", 0x14, 0x1C)

    def test_place_is_idempotent_and_rejects_conflict(self) -> None:
        self.assertEqual(split_edits.place(self.project, "us", "alpha", 0x80001000), [])
        with self.assertRaisesRegex(Held, "alpha"):
            split_edits.place(self.project, "us", "alpha", 0x80009999)
        with self.assertRaisesRegex(Held, "address"):
            split_edits.place(self.project, "us", "new_symbol", None)
        path = self.project.version("us").symbols
        path.write_bytes(b"alpha = 0x80001000;")
        edit = split_edits.place(self.project, "us", "extra", 0x80002000)[0]
        self.assertEqual(edit.after, "alpha = 0x80001000;\nextra = 0x80002000;\n")

    def test_comment_stripping_keeps_quoted_hashes(self) -> None:
        self.assertEqual(
            split_create.without_comments('name: "Game #1" # note\n# whole line\noptions:\n'),
            'name: "Game #1"\noptions:\n',
        )

    def test_measured_main_preserves_binary_without_bss(self) -> None:
        # Both layouts have a short measured main and an asset tail to EOF.
        for start, end, vram in ((0x6000, 0xE140, 0x80076000), (0x103C, 0x2E70, 0x8000043C)):
            with self.subTest(start=start):
                data = bytearray(0x10000)
                data[start : start + 8] = bytes.fromhex("03e0000800000000")
                data[-16:] = bytes.fromhex("27bdfff00000000003e0000827bd0010")
                text = (
                    f"segments:\n  - name: main\n    type: code\n    start: 0x{start:X}\n"
                    f"    vram: 0x{vram:X}\n    subsegments:\n      - [0x{start:X}, asm]\n"
                    f"  - type: bin\n    start: 0x{end:X}\n  - [0x10000]\n"
                )
                self.assertEqual(split_create.complete_executable(text, data), text)
                self.assertEqual(
                    split_analysis.executable_end(data, start, len(data), vram - start), (start + 8 + 15) // 16 * 16
                )

    def test_bss_end_uses_explicit_segment_facts(self) -> None:
        path = self.project.version("us").split
        original = path.read_text()
        for field, value in [("bss_size", "0x20"), ("bss_end", "0x80001234")]:
            with self.subTest(field=field):
                path.write_text(original.replace("    subsegments:", f"    {field}: {value}\n    subsegments:"))
                expected = 0x80001070 if field == "bss_size" else 0x80001234
                self.assertEqual(split.bss_end(self.project, "us", "code"), expected)
        path.write_text(original)
        for name, reason in [("code", "bss_size"), ("missing", "bss_end")]:
            with self.subTest(name=name), self.assertRaisesRegex(Held, reason):
                split.bss_end(self.project, "us", name)


class RowIdentityTests(unittest.TestCase):
    def test_equal_rows_keep_their_own_boundaries(self) -> None:
        # Rows are identified by position; equal-looking rows must not borrow each other's end.
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary).resolve() / "game.yaml"
            path.write_text(
                "segments:\n  - name: main\n    type: code\n    start: 0x40\n    vram: 0x80000400\n"
                "    subalign: 4\n    subsegments:\n      - [0x40, asm, alpha]\n      - [0x48, asm, beta]\n"
                "  - [0x60]\n"
            )
            _, _, segments = split.layout(path)
            alpha, beta = segments[0].rows
            self.assertEqual((split.end(alpha), split.end(beta)), (0x48, 0x60))
            self.assertNotEqual(alpha, dataclasses.replace(alpha))


if __name__ == "__main__":
    unittest.main()
