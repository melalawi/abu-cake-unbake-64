"""Standalone graph and SN64 helper tests; all scratch stays under TMPDIR."""

import dataclasses
import hashlib
import json
import os
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tests.project.makefile_fixture import WORK, fixture, helper, write_rendered
from tests.support import test_policy, tool
from unbake.project import config, makefile


class MakefileTests(unittest.TestCase):
    def setUp(self) -> None:
        WORK.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=WORK)
        self.addCleanup(self.temporary.cleanup)
        self.addCleanup(patch.stopall)
        self.root = Path(self.temporary.name)

    def test_missing_host_tool_is_refused_by_name(self) -> None:
        project, _ = fixture(self.root)
        write_rendered(project)
        (self.root / "tools/splat").unlink()
        result = subprocess.run(["make", "-C", str(self.root), "VERSION=us", "extract"], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("tools/splat: missing executable", result.stderr)

    def test_shared_linker_fragment_requires_explicit_facts(self) -> None:
        script = "SECTIONS {\n/DISCARD/ : { *(*) }\n}"
        for section in (".rdata", ".rodata"):
            with self.subTest(section=section):
                result = makefile.linker_script(
                    script, [dict(object="obj/src/middle.o", section=section, address=0x80001000)]
                )
                self.assertIn("(NOLOAD) : SUBALIGN(1)", result)
                self.assertLess(result.index(".resident_"), result.index("/DISCARD/"))
        with self.assertRaisesRegex(config.Held, "rodata.address"):
            makefile.linker_script(script, [dict(object="obj/src/middle.o", section=".rdata")])

    def test_partial_selection_preserves_nested_paths_and_original_rows(self) -> None:
        module = helper("extract")
        (self.root / "src/nested").mkdir(parents=True)
        (self.root / "src/nested/draft.c").write_text("#ifdef NON_MATCHING\nint draft(void) {return 0;}\n#endif\n")
        original = "      - [0x10, asm, nested/draft]\n      - [0x20, asm, untouched]\n"
        self.assertEqual(
            module.partial_rows(original, self.root / "src"),
            '      - [0x10, c, "nested/draft"]\n      - [0x20, asm, untouched]\n',
        )

    def test_partial_link_bypasses_matching_constant_proof(self) -> None:
        project, _ = fixture(self.root)
        write_rendered(project)
        build = self.root / "build/us.nonmatching"
        obj = build / "obj/src/draft.o"
        obj.parent.mkdir(parents=True)
        code = (
            ".text\n.set noreorder\nlui $2,%hi(pool)\nlw $2,%lo(pool)($2)\njr $31\n"
            "nop\n.section .rdata\npool: .word 0x3f800000\n"
        )
        subprocess.run(
            [tool("mips-linux-gnu-as"), "-EB", "-o", str(obj)], input=code, text=True, capture_output=True, check=True
        )
        script = build / "input.ld"
        script.write_text("SECTIONS {\n.text : { obj/src/draft.o(.text) }\n/DISCARD/ : { *(*) }\n}\n")
        ranges = build / "ranges.json"
        ranges.write_text(json.dumps({"draft": dict(start=0, end=12, address=0x80000000)}))
        command = [
            sys.executable,
            str(self.root / "tools/layout.py"),
            "--script",
            str(script),
            "--output",
            str(build / "link.ld"),
            "--build",
            str(build),
            "--ranges",
            str(ranges),
            "--baserom",
            str(self.root / "baserom.us.z64"),
        ]
        for partial, expected in (("0", False), ("1", True)):
            with self.subTest(partial=partial):
                result = subprocess.run([*command, "--non-matching", partial], capture_output=True, text=True)
                self.assertEqual(result.returncode == 0, expected, result.stderr)
        self.assertIn(".partial_draft_rdata", (build / "link.ld").read_text())
        self.assertNotIn("NOLOAD", (build / "link.ld").read_text())

    def test_missing_recipe_key_refused_by_name(self) -> None:
        project, _ = fixture(self.root)
        (self.root / "config.toml").write_text("[build]\n")
        with self.assertRaisesRegex(config.Held, r"\[build\].as"):
            makefile.render(project)

    def test_sn64_recipe_flags_and_helpers(self) -> None:
        project, _ = fixture(self.root, "sn64")
        rendered = makefile.render(project)
        self.assertIn("tools/asn64.py", rendered)
        self.assertIn("tools/resolve_external_branches.py", rendered)
        self.assertEqual(makefile.flags(project, "us", "src/middle.c"), ("-O2", "-Iinclude", "-DVERSION_US=1"))
        self.assertNotIn("unbake", rendered["Makefile"].replace(str(self.root), "PROJECT"))
        self.assertNotIn("toolkit", rendered["Makefile"])
        self.assertIn("BUILD ?= build/$(VERSION)", rendered["Makefile"])

    def test_standalone_cold_warm_and_header_dependency(self) -> None:
        project, _ = fixture(self.root)
        write_rendered(project)
        generation = self.root / "build/us.1"
        command = ["make", "-C", str(self.root), "-j2", "VERSION=us", "BUILD=" + str(generation)]
        first = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        self.assertIn(str(generation / "game.us.z64") + ": OK", first.stdout)
        calls = (self.root / "calls").read_text().splitlines()
        self.assertEqual(calls.count("splat"), 1)
        second = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertEqual((self.root / "calls").read_text().splitlines(), calls)
        (self.root / "include/value.h").write_text("#define VALUE 2\n")
        third = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(third.returncode, 0, third.stdout + third.stderr)
        changed = (self.root / "calls").read_text().splitlines()
        self.assertEqual(changed.count("splat"), 1)
        self.assertEqual(changed.count("as"), 1)
        self.assertEqual(changed.count("cc"), 2)
        self.assertEqual(changed.count("ld"), 1)

    def test_generation_copy_has_no_old_generation_object_paths(self) -> None:
        module = helper("extract")
        staging = self.root / "scratch"
        script = f"SECTIONS {{ {staging}/asm/nonmatchings/first.s.o(.text); {staging}/assets/tail.bin.o(.data); }}"
        rewritten, graph = module.inventory(script, staging, Path("asm/us"), Path("src"), "sn64")
        self.assertIn("obj/asm/nonmatchings/first.o(.text)", rewritten)
        self.assertNotIn(str(self.root), rewritten)
        self.assertIn("$(BUILD)/obj/asm/nonmatchings/first.built: asm/us/nonmatchings/first.s", graph)

    def test_external_local_address_references_get_exact_link_symbols(self) -> None:
        module = helper("extract")
        (self.root / "first.s").write_text(
            ".L80001000:\n lui $at, %hi(.L80002000)\n lw $v0, %lo(.L80002000)($at)\n beq $v0, $zero, .L80001000\n"
        )
        self.assertEqual(module.external_labels(self.root), ".L80002000 = 0x80002000;\n")

    def test_external_branches_count_zero_operand_instructions_and_words(self) -> None:
        module = helper("resolve_external_branches")
        text = (
            ".globl renamed\n.ent renamed\nrenamed:\n nop\n eret\n .word 0, 1\n beq "
            "$4, $5, target\n jr $31\n.end renamed\n"
        )
        result = module.resolve(text, "renamed", {"renamed": 0x80001000, "target": 0x80001040}, {"renamed"})
        self.assertIn(".word 0x1085000B", result)
        self.assertIn(".word 0x03E00008", result)

    def test_numeric_branch_target_and_trap(self) -> None:
        module = helper("resolve_external_branches")
        result = module.resolve(
            "entry:\n bc1f . + 4 + (-0x2 << 2)\n teq $4, $5, 3\n", "entry", {"entry": 0x80001000}, {"entry"}
        )
        self.assertIn("0x4500FFFE", result)
        self.assertIn("0x008500F4", result)
        with self.assertRaisesRegex(ValueError, "missing"):
            module.resolve("entry:\n beq $4, $5, absent\n", "entry", {"entry": 0x80001000}, {"entry"})

    def test_external_jump_and_hilo_hazards_are_exact_instructions(self) -> None:
        module = helper("resolve_external_branches")
        text = "entry:\n j .L80002000\n nop\n mfhi $6\n div $16, $7\n"
        result = module.resolve(text, "entry", {"entry": 0x80001000}, {"entry"})
        self.assertIn("0x08000800", result)
        self.assertIn("0x00003010", result)
        self.assertIn("0x0207001A", result)

    def test_unit_flag_overrides_share_the_compiler_recipe(self) -> None:
        project, _ = fixture(self.root)
        with (self.root / "config.toml").open("a") as stream:
            stream.write('\n[build.unit_cflags]\nmiddle = ["-O1"]\n')
        self.assertEqual(makefile.flags(project, "us", "src/middle.c")[-1], "-O1")
        self.assertEqual(makefile.flags(project, "us", project.src / "middle.c")[-1], "-O1")
        self.assertEqual(makefile.description(project)["unit_cflags"]["middle"], ("-O1",))

    def test_real_splat_and_binutils_extract_link_and_compare(self) -> None:
        project, _ = fixture(self.root)
        header = bytearray(64)
        struct.pack_into(">4I", header, 0, 0x80371240, 15, 0x80001000, 0)
        header[32:52] = b"BUILD FIXTURE       "
        rom = bytes(header) + b"".join(struct.pack(">3I", 0x24020000 | value, 0x03E00008, 0) for value in (1, 2, 3))
        version = dataclasses.replace(project.version("us"), baserom_sha1=hashlib.sha1(rom).hexdigest())
        version.baserom.write_bytes(rom)
        version.split.write_text(
            "name: Fixture\noptions:\n  basename: game\n  target_path: ../../base"
            "rom.us.z64\n  base_path: ../..\n  platform: n64\n  compiler: IDO\n  f"
            "ind_file_boundaries: false\nsegments:\n  - [0x0, header, header]\n  "
            "- name: main\n    type: code\n    start: 0x40\n    vram: 0x80001000\n"
            "    subalign: 4\n    align: 4\n    subsegments:\n      - [0x40, asm,"
            " alpha]\n      - [0x4C, asm, beta]\n      - [0x58, asm, gamma]\n  - "
            "[0x64]\n"
        )
        version.symbols.write_text("alpha = 0x80001000;\nbeta = 0x8000100C;\ngamma = 0x80001018;\n")
        project = dataclasses.replace(project, version_map={"us": version})
        facts = {
            "as": tool("mips-linux-gnu-as"),
            "ld": tool("mips-linux-gnu-ld"),
            "objcopy": tool("mips-linux-gnu-objcopy"),
            "splat": str(test_policy().splat),
            "asflags": ["-march=vr4300", "-mabi=32", "-EB", "--no-pad-sections"],
        }
        (self.root / "config.toml").write_text(
            "[build]\n" + "".join(k + " = " + json.dumps(v) + "\n" for k, v in facts.items())
        )
        write_rendered(project)
        environment = dict(os.environ)
        environment.pop("PYTHONNOUSERSITE", None)
        result = subprocess.run(["make", "-C", str(self.root), "-j2"], capture_output=True, text=True, env=environment)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        built = (self.root / "build/us/game.us.z64").read_bytes()
        self.assertEqual(built, rom)


class HostExecutableTests(unittest.TestCase):
    def test_policy_references_resolve_through_the_operator_policy(self) -> None:
        policy = SimpleNamespace(cpp=Path("/usr/bin/cpp"))
        self.assertEqual(makefile.host_executable(policy, "policy:cpp", "cpp"), "/usr/bin/cpp")  # type: ignore[arg-type]
        self.assertEqual(makefile.host_executable(policy, "tools/cpp", "cpp"), "tools/cpp")  # type: ignore[arg-type]
        with self.assertRaisesRegex(config.Held, "policy.mips_cpp: missing executable for build.cpp"):
            makefile.host_executable(policy, "policy:mips_cpp", "cpp")  # type: ignore[arg-type]
        with self.assertRaisesRegex(config.Held, "build.cpp: missing value"):
            makefile.host_executable(policy, "", "cpp")  # type: ignore[arg-type]
