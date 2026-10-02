"""Resolved C relocations and split padding contribute to native report counters."""

import json
import os
import struct
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.project.makefile_fixture import fixture
from tests.support import test_policy, tool
from unbake.layout import split
from unbake.project.config import Project, Version
from unbake.report.progress import write
from unbake.report.units import functions, units


class UnitsTests(unittest.TestCase):
    def test_matched_rows_share_one_split_read_with_segment_addresses(self) -> None:
        self.addCleanup(patch.stopall)
        with tempfile.TemporaryDirectory(dir=os.environ["TMPDIR"]) as temporary:
            root = Path(temporary)
            project, _ = fixture(root / "project")
            version = project.version("us")
            version.split.write_text(
                "segments:\n  - name: main\n    type: code\n    start: 0\n"
                "    vram: 0x80000000\n    subalign: 4\n    subsegments:\n"
                "      - [0, c, first]\n      - [4, c, second]\n"
                "  - name: overlay\n    type: code\n    start: 8\n"
                "    vram: 0x80200000\n    subalign: 4\n    subsegments:\n"
                "      - [8, c, third]\n  - [12]\n"
            )
            version.baserom.write_bytes(bytes(12))
            generation = root / "generation"
            generation.mkdir()
            assembly = root / "functions.s"
            assembly.write_text(
                ".text\n.globl first,second\nfirst: .word 0\nsecond: .word 0\n"
                '.section .overlay,"ax"\n.globl third\nthird: .word 0\n'
            )
            obj = root / "functions.o"
            subprocess.run(
                [tool("mips-linux-gnu-as"), "-EB", "--no-pad-sections", "-o", str(obj), str(assembly)],
                capture_output=True,
                check=True,
            )
            subprocess.run(
                [
                    tool("mips-linux-gnu-ld"),
                    "-Ttext",
                    "0x80000000",
                    "--section-start=.overlay=0x80200000",
                    "-e",
                    "first",
                    "-o",
                    str(generation / "game.elf"),
                    str(obj),
                ],
                capture_output=True,
                check=True,
            )
            for name in ("first", "second", "third"):
                (project.src / (name + ".c")).write_text(f"void {name}(void) {{}}\n")
                base = generation / "obj/src" / (name + ".o")
                base.parent.mkdir(parents=True, exist_ok=True)
                base.write_bytes(obj.read_bytes())
            for attempt in range(2):
                with self.subTest(attempt=attempt), patch.object(split, "layout", wraps=split.layout) as read:
                    rows = units(project, test_policy(root), "us", generation, root / "report")
                    self.assertEqual([row["name"] for row in rows], ["first", "second", "third"])
                    self.assertTrue(all(row["metadata"]["complete"] for row in rows))
                    self.assertEqual(read.call_count, 1)

    def test_startup_is_code_and_terminal_bss_is_data(self) -> None:
        with tempfile.TemporaryDirectory(dir=os.environ["TMPDIR"]) as temporary:
            root = Path(temporary)
            version = Version("us", root / "rom", "0" * 40, root / "split.yaml", root / "symbols", ())
            version.split.write_text(
                "segments:\n  - name: main\n    type: code\n    start: 0x1000\n"
                "    vram: 0x80001000\n    subsegments:\n"
                "      - [0x1000, asm, startup]\n      - [0x1038, data, globals]\n"
                "      - [0x1060, asm, program]\n      - [0x1070, bss, bss]\n  - [0x1070]\n"
            )
            measured = functions(version)
            self.assertEqual([(row.name, row.end - row.start) for row in measured], [("startup", 56), ("program", 16)])
            version.split.write_text(version.split.read_text().replace("data, globals", "asm, globals"))
            self.assertEqual(len(functions(version)), 3)

    def test_linked_relocations_and_padding_use_split_totals(self) -> None:
        with tempfile.TemporaryDirectory(dir=os.environ["TMPDIR"]) as temporary:
            root = Path(temporary)
            version = Version(
                name="us",
                baserom=root / "baserom.us.z64",
                baserom_sha1="0" * 40,
                split=root / "split.yaml",
                symbols=root / "symbols.txt",
                macros=(),
                cartridge_id="NUS-TEST-0",
                region="Test region",
                description="Test release.",
            )
            project = Project(
                root=root,
                name="fixture",
                title="Fixture",
                names_from="us",
                versions=("us",),
                src=root / "src",
                include=(),
                asm=root / "asm",
                tools=root / "tools",
                compilers={},
                default_compiler="fixture",
                units={},
                version_map={"us": version},
                id="00000000-0000-4000-8000-000000000001",
                workspace_id="00000000-0000-4000-8000-000000000002",
                roms=root / "roms",
                build=root / "build",
                work=root / "build/work",
                drafts=root / "build/drafts",
            )
            project.src.mkdir()
            policy = test_policy(root)
            generation = project.root / "build/us.1"
            generation.mkdir(parents=True)
            project.build_link("us").symlink_to(generation.name)
            version = project.version("us")
            version.split.write_text(
                "segments:\n  - name: text\n    type: code\n    start: 0\n"
                "    vram: 0x80000000\n    subalign: 4\n    subsegments:\n"
                "      - [0, c, matched]\n      - [16, asm, padding]\n  - [32]\n"
            )
            (project.src / "matched.c").write_text("void matched(void) {}\n")
            readme = project.root / "README.md"
            readme.write_text("## Progress\n\n| us (fixture) |\n\n## End\n")
            for name, directory, body in (
                ("matched", "src", "lui $v0,%hi(external)\naddiu $v0,$v0,%lo(external)\njr $ra\nnop\n"),
                ("padding", "asm", "addiu $v0,$zero,1\njr $ra\nnop\n"),
            ):
                destination = generation / "obj" / directory / (name + ".o")
                destination.parent.mkdir(parents=True, exist_ok=True)
                assembly = (
                    f".text\n.set noreorder\n.globl {name}\n.type {name},@function\n{name}:\n"
                    + body
                    + f".size {name},.-{name}\n"
                    + (".word 0\n" if name == "padding" else "")
                )
                subprocess.run(
                    [tool("mips-linux-gnu-as"), "-EB", "-mips3", "--no-pad-sections", "-o", str(destination)],
                    input=assembly,
                    text=True,
                    capture_output=True,
                    check=True,
                )
            subprocess.run(
                [
                    tool("mips-linux-gnu-ld"),
                    "-Ttext",
                    "0x80000000",
                    "--defsym",
                    "external=0x80008000",
                    "-e",
                    "matched",
                    "-o",
                    str(generation / "game.elf"),
                    str(generation / "obj/src/matched.o"),
                ],
                capture_output=True,
                check=True,
            )
            words = (0x3C028001, 0x24428000, 0x03E00008, 0, 0x24020001, 0x03E00008, 0, 0)
            for changed, matched in ((False, 16), (True, 0)):
                with self.subTest(changed=changed):
                    version.baserom.write_bytes(struct.pack(">8I", words[0] ^ int(changed), *words[1:]))
                    write(project, policy)
                    measures = json.loads((project.root / "versions/us/report.json").read_text())["measures"]
                    self.assertEqual(int(measures.get("matched_code", 0)), matched)
                    self.assertEqual(measures["complete_code"], 16)
                    self.assertEqual(measures["total_code"], 32)
                    self.assertIn(
                        "bytes     [██████████░░░░░░░░░░]  50.00% (~49.88%)  16 of 32"
                        if changed
                        else "bytes     [██████████░░░░░░░░░░]  50.00% (~50.00%)  16 of 32",
                        readme.read_text(),
                    )
