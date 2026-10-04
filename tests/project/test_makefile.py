"""Standalone graph and SN64 helper tests; all scratch stays under TMPDIR."""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tests.project.makefile_fixture import WORK, fixture, helper, write_rendered
from unbake.project import config, makefile


class MakefileTests(unittest.TestCase):
    def setUp(self) -> None:
        WORK.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=WORK)
        self.addCleanup(self.temporary.cleanup)
        self.addCleanup(patch.stopall)
        self.root = Path(self.temporary.name).resolve()

    def test_missing_host_tool_is_refused_by_name(self) -> None:
        project, _ = fixture(self.root, case=self)
        write_rendered(project)
        (self.root / "tools/splat").unlink()
        from unbake.project_tools.host import resolve_tool

        with (
            patch("pathlib.Path.cwd", return_value=self.root),
            self.assertRaisesRegex(ValueError, "missing executable"),
        ):
            resolve_tool("tools/splat")

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
        project, _ = fixture(self.root, case=self)
        write_rendered(project)
        build = self.root / "build/us.nonmatching"
        obj = build / "obj/src/draft.o"
        obj.parent.mkdir(parents=True)
        code = (
            ".text\n.set noreorder\nlui $2,%hi(pool)\nlw $2,%lo(pool)($2)\njr $31\n"
            "nop\n.section .rdata\npool: .word 0x3f800000\n"
        )
        from tests.assembly_fixture import object_fixture
        from tests.helper_fixture import call
        from unbake.project_tools import layout

        object_fixture(obj, code)
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
            str(self.root / "roms/baserom.us.z64"),
        ]
        for partial, expected in (("0", False), ("1", True)):
            with self.subTest(partial=partial):
                result = call(layout, [*command[2:], "--non-matching", partial], self.root)
                self.assertEqual(result.returncode == 0, expected, result.stderr)
        self.assertIn(".partial_draft_rdata", (build / "link.ld").read_text())
        self.assertNotIn("NOLOAD", (build / "link.ld").read_text())

    def test_missing_recipe_key_refused_by_name(self) -> None:
        project, _ = fixture(self.root, case=self)
        (self.root / "config.toml").write_text("[build]\n")
        with self.assertRaisesRegex(config.Held, r"\[build\].as"):
            makefile.render(project)

    def test_sn64_recipe_flags_and_helpers(self) -> None:
        project, _ = fixture(self.root, "sn64", case=self)
        rendered = makefile.render(project)
        self.assertIn("tools/sn64_cc.py", rendered)
        self.assertIn("tools/resolve_external_branches.py", rendered)
        self.assertEqual(
            makefile.flags(project, "us", "src/middle.c"),
            ("-Iinclude", "-O2", "-DVERSION_US=1"),
        )
        self.assertNotIn("unbake", rendered["Makefile"].replace(str(self.root), "PROJECT"))
        self.assertNotIn("toolkit", rendered["Makefile"])
        self.assertIn("BUILD ?= build/$(VERSION)", rendered["Makefile"])

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
        project, _ = fixture(self.root, case=self)
        with (self.root / "config.toml").open("a") as stream:
            stream.write('\n[build.unit_cflags]\nmiddle = ["-O1"]\n')
        self.assertEqual(makefile.flags(project, "us", "src/middle.c")[-1], "-O1")
        self.assertEqual(makefile.flags(project, "us", project.src / "middle.c")[-1], "-O1")
        self.assertEqual(makefile.description(project)["unit_cflags"]["middle"], ("-O1",))


class HostExecutableTests(unittest.TestCase):
    def test_policy_references_resolve_through_the_operator_policy(self) -> None:
        policy = SimpleNamespace(cpp=Path("/usr/bin/cpp"))
        self.assertEqual(makefile.host_executable(policy, "policy:cpp", "cpp"), "/usr/bin/cpp")  # type: ignore[arg-type]
        self.assertEqual(makefile.host_executable(policy, "tools/cpp", "cpp"), "tools/cpp")  # type: ignore[arg-type]
        with self.assertRaisesRegex(config.Held, "policy.mips_cpp: missing executable for build.cpp"):
            makefile.host_executable(policy, "policy:mips_cpp", "cpp")  # type: ignore[arg-type]
        with self.assertRaisesRegex(config.Held, "build.cpp: missing value"):
            makefile.host_executable(policy, "", "cpp")  # type: ignore[arg-type]
