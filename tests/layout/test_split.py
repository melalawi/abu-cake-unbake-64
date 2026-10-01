"""Split edits against small layouts and a fake compare/build boundary."""

import dataclasses
import json
import os
import shlex
import tempfile
import unittest
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from unbake.layout import split, split_analysis, split_apply, split_create, split_edits, split_partition
from unbake.project import build
from unbake.project.config import Held, Policy, Project
from unbake.project.fingerprint import Counts, Region


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

    def matched(self, function: str, versions: Sequence[str]) -> None:
        path = self.root / "data" / "matched.jsonl"
        path.parent.mkdir(exist_ok=True)
        path.write_text(
            json.dumps(
                {"function": function, "versions": versions, "sha256": "a" * 64, "at": "2026-01-01T00:00:00+00:00"}
            )
            + "\n"
        )

    def generations(self) -> None:
        directory = self.root / "build"
        directory.mkdir()
        for v in self.versions:
            generation = directory / f"{v}.0"
            generation.mkdir()
            (generation / "untouched").write_text(v)
            self.build_link(v).symlink_to(generation.name)


@contextmanager
def fake_build(
    project: ProjectFixture, *, failing: str | None = None, error: Exception | None = None
) -> Iterator[list[tuple[Sequence[str], Path]]]:
    calls = []

    def run(
        project: Project, policy: Policy, versions: Sequence[str], *, tree: Path, generation_for: Callable[[str], Path]
    ) -> dict[str, SimpleNamespace]:
        calls.append((versions, tree))
        if error:
            raise error
        results = {}
        for v in versions:
            generation = generation_for(v)
            (generation / "checked").write_text(split.read(project.version(v).symbols))
            results[v] = SimpleNamespace(
                version=v,
                ok=v != failing,
                sha1_line=f"{v}: {'FAIL' if v == failing else 'OK'}",
                log=generation / "log",
                generation=generation,
            )
        return results

    with patch.object(build, "build", side_effect=run):
        yield calls


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

    def test_rename_exact_symbol_across_versions_preserves_metadata(self) -> None:
        version = self.project.version("us")
        version.symbols.write_text(
            split.read(version.symbols).replace("alpha = 0x80001000;", "  alpha  = 0x80001000; // type:func size:0x10")
            + "alpha_extra = 0x80005000;\n"
        )
        edits = split_edits.rename(self.project, "alpha", "entry")
        self.assertEqual(len(edits), 4)
        self.assertEqual({v for edit in edits for v in edit.versions}, {"us", "eu"})
        symbol = next(edit for edit in edits if edit.path == version.symbols)
        self.assertIn("  entry  = 0x80001000; // type:func size:0x10", symbol.after)
        self.assertIn("alpha_extra =", symbol.after)
        self.assertIn("alpha =", split.read(self.project.version("eu").symbols))

    def test_rename_symbol_whose_row_has_different_stem(self) -> None:
        version = self.project.version("us")
        version.symbols.write_text(split.read(version.symbols).replace("alpha =", "callable ="))
        edits = split_edits.rename(self.project, "callable", "entry")
        self.assertEqual(len(edits), 2)
        self.assertIn("[0x000010, asm, entry]", edits[0].after)

    def test_rename_refuses_missing_or_colliding_name(self) -> None:
        for old, new in (("alpha", "beta"), ("missing", "entry"), ("alpha", "alpha"), (None, "entry")):
            with self.subTest(old=old, new=new), self.assertRaises(Held):
                split_edits.rename(self.project, old, new)

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

    def test_twins_rename_equal_words_from_matched_other_version(self) -> None:
        self.project.layout("us", [(0x10, "c", "alpha"), (0x20, "asm", "beta"), (0x40, "data", "pool")])
        self.project.layout("eu", [(0x10, "asm", "different"), (0x20, "asm", "beta"), (0x40, "data", "pool")])
        self.project.matched("alpha", ["us"])
        edits = split_edits.twins(self.project, "us", "alpha")
        self.assertEqual(len(edits), 2)
        self.assertTrue(all(edit.versions == ("eu",) for edit in edits))
        self.assertIn("[0x000010, asm, alpha]", edits[0].after)
        self.assertIn("alpha =", edits[1].after)

    def test_twins_requires_matched_c_and_rejects_ambiguous_words(self) -> None:
        with self.assertRaisesRegex(Held, "not matched"):
            split_edits.twins(self.project, "us", "alpha")
        self.project.matched("alpha", ["us"])
        with self.assertRaisesRegex(Held, "c row"):
            split_edits.twins(self.project, "us", "alpha")
        self.project.layout("us", [(0x10, "c", "alpha"), (0x20, "data", "pool")])
        self.project.layout("eu", [(0x10, "asm", "one"), (0x20, "asm", "two"), (0x30, "data", "pool")])
        rom = self.project.version("eu").baserom
        content = bytearray(rom.read_bytes())
        content[0x20:0x30] = content[0x10:0x20]
        rom.write_bytes(content)
        with self.assertRaisesRegex(Held, "ambiguous"):
            split_edits.twins(self.project, "us", "alpha")

    def test_twins_no_candidate_returns_empty(self) -> None:
        self.project.layout("us", [(0x10, "c", "alpha"), (0x20, "data", "pool")])
        self.project.layout("eu", [(0x10, "asm", "other"), (0x20, "data", "pool")])
        self.project.version("eu").baserom.write_bytes(b"\xff" * 128)
        self.project.matched("alpha", ["us"])
        self.assertEqual(split_edits.twins(self.project, "us", "alpha"), [])

    def test_apply_builds_every_affected_version_and_publishes_after_success(self) -> None:
        self.project.generations()
        edits = split_edits.rename(self.project, "alpha", "entry")
        with fake_build(self.project) as calls:
            results = split_apply.apply(self.project, self.project.policy, edits)
        self.assertEqual(calls, [(["us", "eu"], self.project.root)])
        self.assertEqual([result.version for result in results], ["us", "eu"])
        for v in self.project.versions:
            self.assertEqual(os.readlink(self.project.build_link(v)), f"{v}.1")
            self.assertEqual((self.project.root / "build" / f"{v}.0" / "untouched").read_text(), v)
            self.assertFalse((self.project.root / "build" / f"{v}.0" / "checked").exists())
        for edit in edits:
            self.assertEqual(split.read(edit.path), edit.after)

    def test_apply_fail_restores_files_and_leaves_build_links(self) -> None:
        self.project.generations()
        edits = split_edits.rename(self.project, "alpha", "entry")
        with fake_build(self.project, failing="eu"):
            results = split_apply.apply(self.project, self.project.policy, edits)
        self.assertEqual([result.ok for result in results], [True, False])
        for edit in edits:
            self.assertEqual(split.read(edit.path), edit.before)
        for v in self.project.versions:
            self.assertEqual(os.readlink(self.project.build_link(v)), f"{v}.0")
            self.assertTrue((self.project.root / "build" / f"{v}.1").exists())

    def test_apply_exception_also_restores_files(self) -> None:
        self.project.generations()
        edits = split_edits.place(self.project, "us", "extra", 0x80005555)
        with fake_build(self.project, error=Held("build", "compiler missing")), self.assertRaises(Held):
            split_apply.apply(self.project, self.project.policy, edits)
        self.assertEqual(split.read(edits[0].path), edits[0].before)

    def test_apply_refuses_stale_edit_before_build(self) -> None:
        edits = split_edits.place(self.project, "us", "extra", 0x80005555)
        edits[0].path.write_text(edits[0].before + "later = 0x80007777;\n")
        with fake_build(self.project) as calls, self.assertRaisesRegex(Held, "changed since"):
            split_apply.apply(self.project, self.project.policy, edits)
        self.assertEqual(calls, [])

    def test_apply_empty_is_noop_and_conflicting_edits_held(self) -> None:
        self.assertEqual(split_apply.apply(self.project, self.project.policy, []), [])
        first = split_edits.place(self.project, "us", "extra", 0x80005555)[0]
        second = split.Edit(first.path, first.before, "different", ("us",))
        with self.assertRaisesRegex(Held, "conflicting"):
            split_apply.diff([first, second])

    def test_cut_segments_partitions_mixed_compilers_and_keeps_data_and_options(self) -> None:
        text = split.read(self.project.version("us").split)
        text = text.replace("    subsegments:", "    bss_size: 0x20\n    bss_end: 0x80001070\n    subsegments:")
        regions = [
            Region(0x80001000, 0x80001010, "gcc", Counts(8, 0), "main"),
            Region(0x80001010, 0x80001028, "ido", Counts(0, 8), "ido"),
            Region(0x80001028, 0x80001050, "gcc", Counts(8, 0), "main_2"),
        ]
        output = split_partition.cut_segments(text, regions)
        self.assertEqual(text.split("segments:")[0], output.split("segments:")[0])
        self.assertIn("  - name: ido\n    type: code\n    start: 0x20\n    vram: 0x80001010", output)
        self.assertIn("  - name: main_2", output)
        self.assertEqual(output.count("bss_end: 0x80001070"), 1)
        self.assertEqual(output.count("bss_size: 0x20"), 1)
        self.assertIn("[0x000040, data, pool]", output)
        self.assertEqual(output.count("asm, alpha"), 1)
        self.assertEqual(output.count("asm, beta"), 1)
        self.assertEqual(output.count("asm, gamma"), 1)

    def test_partition_updates_following_binary_vram_dependency(self) -> None:
        text = split.read(self.project.version("us").split)
        text = text.replace(
            "  - [0x000060]", "  - type: bin\n    start: 0x000060\n    follows_vram: code\n  - [0x000100]"
        )
        regions = [
            Region(0x80001000, 0x80001010, "gcc", Counts(8, 0), "main"),
            Region(0x80001010, 0x80001050, None, Counts(0, 0), "tail"),
        ]
        output = split_partition.cut_segments(text, regions)
        self.assertIn("follows_vram: tail\n", output)

    def test_cut_segments_refuses_unmeasured_boundary(self) -> None:
        text = split.read(self.project.version("us").split)
        regions = [
            Region(0x80001000, 0x80001008, "gcc", Counts(8, 0), "main"),
            Region(0x80001008, 0x80001050, "ido", Counts(0, 8), "ido"),
        ]
        with self.assertRaisesRegex(Held, "no subsegment at ROM 0x18"):
            split_partition.cut_segments(text, regions)

    def test_file_split_cuts_only_at_measured_compiler_boundaries(self) -> None:
        self.project.layout("us", [(0x10, "asm", "whole"), (0x40, "data", "pool")])
        text = split.read(self.project.version("us").split)
        function = split.Function("us", "beta", 0x20, 0x40, 0x80001010, "whole", "asm", ())
        regions = [
            Region(0x80001000, 0x80001010, "gcc", Counts(8, 0), "main"),
            Region(0x80001010, 0x80001050, "ido", Counts(0, 8), "ido", (function,)),
        ]
        output = split_partition.cut_segments(text, regions)
        self.assertIn("[0x000010, asm, whole]", output)
        self.assertIn('[0x20, asm, "whole_ido"]', output)
        self.assertIn("[0x000040, data, pool]", output)

    def test_comment_stripping_keeps_quoted_hashes(self) -> None:
        self.assertEqual(
            split_create.without_comments('name: "Game #1" # note\n# whole line\noptions:\n'),
            'name: "Game #1"\noptions:\n',
        )

    def test_create_full_executable_and_title(self) -> None:
        from tests.support import test_policy

        for title in ("Game: One", "Game #1"):
            with self.subTest(title=title):
                source = self.project.root / "cartridge.z64"
                data = bytearray(0x1200)
                data[:4] = bytes.fromhex("80371240")
                data[8:12] = bytes.fromhex("80000400")
                data[0x20:0x34] = title.encode().ljust(20, b" ")
                data[0x3B:0x40] = b"NUGE\0"
                # Entry clears BSS at 0x80000600, then jumps to main.
                entry = bytes.fromhex(
                    "3c088000250806003c09000025290020ad000000210800082129fff81520fffc000000003c0a8000254a04400340000800000000"
                )
                data[0x1000 : 0x1000 + len(entry)] = entry
                # Direct call crosses a noninstruction that truncates Splat.
                data[0x1040:0x1058] = bytes.fromhex("27bdfff00c000130000000008fbf000c03e0000827bd0010")
                data[0x1058:0x105C] = bytes.fromhex("ffffffff")
                data[0x10C0:0x10D0] = bytes.fromhex("27bdfff02402000103e0000827bd0010")
                source.write_bytes(data)
                policy = test_policy()
                launcher = self.project.root / "splat"
                launcher.write_text(
                    "#!/bin/sh\nunset PYTHONNOUSERSITE\nexec " + shlex.quote(str(policy.splat)) + ' "$@"\n'
                )
                launcher.chmod(0o755)
                output = split_create.create(source, "game", "us", policy=replace(policy, splat=launcher))
                self.assertIn("vram: 0x80000440", output)
                self.assertIn("target_path: roms/baserom.us.z64", output)
                self.assertIn("symbol_addrs_path: versions/us/symbol_addrs.txt", output)
                self.assertIn("[0x10D0, data, data_0010D0]", output)
                self.assertIn("name: " + json.dumps(title), output)
                self.assertIn("bss_end: 0x80000620", output)
                self.assertFalse(list(self.project.root.glob(".create-*")))

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

    def test_creation_headers_and_rollback(self) -> None:
        for location, failure, expected in [
            ("src/new.c", None, ["us"]),
            ("include/new.h", None, ["us", "eu"]),
            ("src/new.c", "us", ["us"]),
            ("include/new.h", "eu", ["us", "eu"]),
        ]:
            with self.subTest(location=location, failure=failure), tempfile.TemporaryDirectory() as temporary:
                project = ProjectFixture(Path(temporary))
                project.generations()
                path = project.root / location
                edit = split.Edit(path, "", "int value;\n", ("us",))
                with fake_build(project, failing=failure) as calls:
                    split_apply.apply(project, project.policy, [edit])
                self.assertEqual(calls[0][0], expected)
                self.assertEqual(path.exists(), failure is None)
        path = self.project.src / "new.c"
        split_apply._write_staging(self.project, [split.Edit(path, "", "int value;", ("us",))])
        self.assertEqual(path.read_text(), "int value;")
        outside = self.project.root / "outside.c"
        with self.assertRaisesRegex(Held, "configured"):
            split_apply._write_staging(self.project, [split.Edit(outside, "", "x", ("us",))])

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
            path = Path(temporary) / "game.yaml"
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
