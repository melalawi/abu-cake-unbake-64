"""Real flow failures replay at the native boundary without spawning tools."""

import hashlib
import importlib.machinery
import io
import json
import os
import struct
import subprocess
import sys
from contextlib import chdir, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from tests.elf_fixture import write_object
from tests.fixture import make_rom
from tests.integration import project as tools
from tests.kit import TESTS
from tests.project_fixture import ProjectCase
from unbake import build, config, process, runner
from unbake.cli import output
from unbake.cycle.events import Emitter
from unbake.layout import split
from unbake.objects.elf import Object
from unbake.report import progress
from unbake.work.score import measure_words


class NativeFlowContractTests(ProjectCase):
    versions = ("us", "eu")

    def setUp(self):
        super().setUp()
        for version in self.versions:
            root = self.project.root / "versions" / version
            (root / "symbols.ld").write_text("PROVIDE(alpha = 0x80001000);\n")
            (root / f"{self.project.name}.ld").write_text("SECTIONS { .text : { *(.text) } }\n")
        self.native_calls = []

    def invoke(self, script, argv, cwd):
        """Execute the fake tool's bytes at the mocked native process seam."""
        stdout, stderr = io.StringIO(), io.StringIO()
        # make check intentionally removes PYTHONPATH; a tool starts with its
        # own directory and interpreter libraries, not the test runner's cwd.
        search = [p for p in sys.path if p and Path(p).resolve() not in (tools.REPO, tools.SRC)]
        with (
            patch.object(sys, "argv", list(map(str, argv))),
            patch.object(sys, "path", search),
            patch.dict(os.environ, build.environment(self.host), clear=True),
            chdir(cwd),
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            try:
                exec(compile(script, str(argv[0]), "exec"), {"__name__": "__main__"})
            except SystemExit as stop:
                self.assertIn(stop.code, (0, None), stderr.getvalue())
            visible = importlib.machinery.PathFinder.find_spec("tests", sys.path) is not None
        return stdout.getvalue(), stderr.getvalue(), visible

    def native(self, argv, cwd, phase, **kwargs):
        self.native_calls.append((list(argv), dict(kwargs.get("context", {}))))
        script = {"n64link": tools.N64LINK, "mips_ld": tools.LD, "mips_objcopy": tools.OBJCOPY, "cc": tools.CC}[
            Path(argv[0]).name
        ]
        stdout, stderr, _ = self.invoke(script, argv, cwd)
        return process.NativeResult(
            tuple(argv),
            str(cwd),
            0,
            None,
            stdout,
            stderr,
            "exit",
            None,
            "utf-8",
            "surrogateescape",
            kwargs.get("context", {}),
        )

    def test_actual_place_segment_recipe_preserves_object_and_version_bindings(self):
        code = struct.pack(">3I", 0x24020001, 0x03E00008, 0)
        original = write_object(self.root / "unit.o", {".text": code}, [("alpha", ".text", 0, 12)])
        before = original.read_bytes()
        with patch.object(process, "run_native", side_effect=self.native):
            for version in self.versions:
                row = split.functions(self.project, version)[0]
                destination = self.root / (version + ".placed.o")
                self.assertEqual(
                    runner.place(self.project, self.host, original, version, row, destination, score=True), []
                )
                argv, context = self.native_calls[-1]
                self.assertEqual(argv[1:3], ["place", str(original)])
                self.assertEqual(
                    argv[argv.index("--text") + 1], f"0x{row.address:X}:0x{row.start:X}:0x{row.end - row.start:X}"
                )
                self.assertEqual(
                    [argv[i + 1] for i, a in enumerate(argv) if a == "--map"], runner.windows(self.project, version)
                )
                self.assertEqual(context["version"], version)
                self.assertEqual(destination.read_bytes(), before)
        self.assertEqual(len(self.native_calls), 2)
        self.assertEqual(original.read_bytes(), before)

    def test_compiler_template_emits_real_elf_for_integer_return_and_typedef_spelling(self):
        from unbake.compilers import drivers

        for spelling, value in (("int", 1), ("s32", 9)):
            with self.subTest(spelling=spelling):
                (self.root / "alpha.i").write_text(f"typedef int s32; {spelling} alpha(void) {{ return {value}; }}\n")
                recipe = drivers.steps(self.project, "us", "alpha", "src/alpha.c", runner.tools(self.host))
                self.invoke(tools.CC, [str(self.project.compiler_for("alpha").cc), *recipe.compile[1:]], self.root)
                obj = Object(self.root / "alpha.o")
                self.assertEqual(
                    obj.content(obj.section(".text")), struct.pack(">3I", 0x24020000 | value, 0x03E00008, 0)
                )
                self.assertEqual([s["name"] for table in obj.symbols.values() for s in table if s["name"]], ["alpha"])

    def test_native_dependencies_use_ido_one_filename_per_rule_and_keep_proved_source(self):
        source = self.project.src / "alpha.c"
        source.write_text("int alpha(void) {return 1;}\n")
        header = self.project.include[0] / "types.h"
        cpp_output = f"alpha.o: {source} \\\n {header}\n"
        with (
            patch("subprocess.check_output", return_value=cpp_output) as cpp,
            patch.object(process, "run_native", side_effect=self.native),
        ):
            dependencies = runner.dependencies(self.project, self.host, source, "us", unit="alpha")
        self.assertEqual(dependencies, {header})
        argv = cpp.call_args.args[0]
        self.assertEqual(argv[0], "/usr/bin/cpp")
        self.assertIn("-M", argv)
        self.assertEqual(argv[-1], str(source))
        self.assertEqual(cpp.call_count, 1)
        self.assertEqual(len(self.native_calls), 1)

    def test_link_and_extract_use_actual_elf_and_exact_or_mismatch_bytes(self):
        row = split.functions(self.project, "us")[0]
        for value in (1, 9):
            with self.subTest(value=value):
                code = struct.pack(">3I", 0x24020000 | value, 0x03E00008, 0)
                obj = write_object(self.root / "placed.o", {".text": code}, [("alpha", ".text", 0, 12)])
                with patch.object(process, "run_tool", side_effect=lambda *a, **k: self.native(*a, **k).stdout):
                    linked = runner.link(
                        self.project, self.host, obj, "us", row, self.root, self.project.src / "alpha.c"
                    )
                self.assertEqual(linked, code)
                measured = measure_words("us", split.words(self.project, row), linked)
                self.assertEqual(measured.exact, value == 1)
                self.assertEqual(measured.target_words, 3)
                self.assertEqual(measured.target_words_different, 0 if value == 1 else 1)
                self.assertEqual(self.native_calls[-1][0][-4:-2], ["-j", ".text"])
        self.assertEqual(len(self.native_calls), 4)

    def test_make_clean_environment_tool_resolves_its_fixture_dependencies(self):
        self.assertNotIn("PYTHONPATH", build.environment(self.host))
        self.assertNotIn(str(tools.REPO), build.environment(self.host)["PATH"])
        _, _, visible = self.invoke(tools.CC, ["cc", "-show", "alpha.c"], self.root)
        self.assertTrue(visible, "the exact make environment cannot import the fake tool's ELF writer")

    def test_actual_build_check_runs_all_versions_in_clean_environment_and_reuses_recipe(self):
        from unbake import buildfiles

        make_calls = []
        pins = []

        def native(argv, cwd, phase, **kwargs):
            if Path(argv[0]).name == "n64link":
                self.assertEqual(argv[1:], ["--version"])
                pins.append(argv)
                stdout = buildfiles.N64LINK_RELEASE
            else:
                self.assertEqual(argv, build.make_command(self.host, "check"))
                self.assertEqual(kwargs["context"]["versions"], list(self.versions))
                self.assertNotIn("PYTHONPATH", kwargs["env"])
                self.assertEqual(kwargs["env"]["PATH"], os.pathsep.join(map(str, self.host.tool_path)))
                self.assertTrue((self.project.root / "Makefile").is_file())
                make_calls.append(argv)
                stdout = "us ROM identical\neu ROM identical\n"
            return process.NativeResult(
                tuple(argv),
                str(cwd),
                0,
                None,
                stdout,
                "",
                "exit",
                None,
                "utf-8",
                "surrogateescape",
                kwargs.get("context", {}),
            )

        with (
            patch.object(process, "run_native", side_effect=native),
            patch("subprocess.run", return_value=subprocess.CompletedProcess([], 1, "", "")) as probe,
        ):
            for _ in range(2):
                outcome = build.check(self.project, self.host, files_only=False)
                self.assertTrue(outcome.ok and outcome.built)
            self.assertEqual(probe.call_count, 2)
        self.assertEqual(len(make_calls), 2)
        self.assertEqual(len(pins), 1)

    def test_actual_build_check_rejects_source_rule_before_any_native_work(self):
        source = self.project.src / "alpha.c"
        source.write_text("#define NULL ((void *)0)\nint alpha(void) {return 1;}\n")
        with patch.object(process, "run_native", side_effect=AssertionError("native work on refused source")) as native:
            outcome = build.check(self.project, self.host, files_only=False)
        self.assertFalse(outcome.ok or outcome.built)
        self.assertEqual(outcome.preflight["key"], "check.source_rules")
        self.assertEqual(outcome.preflight["work"], {"source_scans": 1, "step_runs": 0, "make_invocations": 0})
        native.assert_not_called()

    def test_actual_fixture_cartridge_labels_bind_each_supplied_rom_hash(self):
        import shutil

        root = self.root / "cartridges"
        shutil.copytree(TESTS / "fixture", root, ignore=shutil.ignore_patterns("__pycache__"))
        make_rom.write_roms(root)
        project = config.load(root)
        descriptions = progress.readme_descriptions(project)
        self.assertEqual(set(descriptions), {"us", "us-rev1"})
        for version in project.versions:
            supplied = project.version(version)
            raw = supplied.baserom.read_bytes()
            self.assertEqual(supplied.cartridge_id, raw[0x3B:0x3F].decode())
            self.assertIn(hashlib.sha256(raw).hexdigest(), descriptions[version])
        self.assertNotEqual(descriptions["us"], descriptions["us-rev1"])

    def test_cycle_event_prefix_and_final_public_result_keep_distinct_current_schemas(self):
        stream = io.StringIO()
        emitter = Emitter(stream)
        emitter.emit("fn.committed", function="gamma", commit="accepted", message="Match gamma")
        emitter.emit("cycle.end", landed=["gamma"], landed_bytes=12, held=[], carryovers=[], exit=0, next=None)
        result = output.Result.ok("cycle", {"landed": ["gamma"], "exit": 0}, [], None)
        self.assertEqual(output.emit(result, stdout=stream, stderr=io.StringIO()), 0)
        records = [json.loads(line) for line in stream.getvalue().splitlines()]
        self.assertEqual([r["event"] for r in records[:-1]], ["fn.committed", "cycle.end"])
        self.assertEqual([r["seq"] for r in records[:-1]], [1, 2])
        self.assertEqual((records[-1]["command"], records[-1]["status"]), ("cycle", "ok"))
        self.assertEqual(records[-1]["data"]["landed"], ["gamma"])
