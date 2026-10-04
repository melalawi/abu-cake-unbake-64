"""Installed trials compare whole native intervals and retain all global entries."""

import hashlib
import json
import os
import shutil
import struct
import subprocess
import sysconfig
import tempfile
import unittest
from pathlib import Path

import toml

from tests.decomp.support import assemble, assembly
from tests.process_fakes import cli_process
from tests.support import test_policy
from unbake.layout import map, split
from unbake import config
from unbake.project import makefile, toolchain


class MultiEntryCliTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name).resolve()
        self.root = self.directory / "project"
        shutil.copytree(Path(__file__).parents[1] / "fixture", self.root)
        self.policy = test_policy()
        (self.root / "README.md").write_text(
            "# Fixture\n\n## Progress\n\n| us (Fixture) |\n|---|\n\n"
            "| us-rev1 (Revision) |\n|---|\n\n## Building\n\nmake check\n"
        )
        data = toml.loads((self.root / "config.toml").read_text())
        data["compilers"]["ido-7.1"]["cflags"] = ["-c", "-G0", "-non_shared", "-mips2", "-O2", "-Iinclude"]
        data["build"]["splat"] = "policy:splat"
        for version in data["project"]["versions"]:
            data["version"][version].update(
                cartridge_id="TEST-" + version, region="Test", description="Fixture release."
            )
        self.words = [0xAC800000, 0x03E00008, 0xAC800004, 0xA4800344, 0xA080034A, 0x03E00008, 0xA0800348]
        for version in data["project"]["versions"]:
            header = bytearray(0x40)
            header[:4] = bytes.fromhex("80371240")
            header[0x20:0x34] = b"ENTRY TEST".ljust(20, b" ")
            header[0x3B:0x3F] = b"NETE"
            image = bytes(header) + struct.pack(">7I", *self.words) + bytes(4)
            rom = self.root / "roms" / f"baserom.{version}.z64"
            rom.parent.mkdir(exist_ok=True)
            rom.write_bytes(image)
            data["version"][version]["baserom_sha1"] = hashlib.sha1(image).hexdigest()
            definitions = self.root / "versions" / version
            (definitions / "fixture.sha1").write_text(hashlib.sha1(image).hexdigest() + f"  fixture.{version}.z64\n")
            (definitions / "fixture.yaml").write_text(
                "name: fixture\noptions:\n  basename: fixture\n  platform: n64\n  compiler: IDO\n"
                "  find_file_boundaries: False\nsegments:\n  - [0x0, header, header]\n"
                "  - name: main\n    type: code\n    start: 0x40\n    vram: 0x80001000\n"
                "    align: 4\n    subalign: 4\n    subsegments:\n      - [0x40, asm, alpha]\n"
                "  - [0x5C, bin, trailer]\n  - [0x60]\n"
            )
            (definitions / "symbol_addrs.txt").write_text(
                "alpha = 0x80001000; // type:func\ntail = 0x8000100C; // type:func\n"
            )
            generation = self.root / "build" / f"{version}.0"
            objects = generation / "obj/asm"
            objects.mkdir(parents=True)
            assemble(objects, "alpha", assembly("alpha", self.words[:3]) + assembly("tail", self.words[3:]))
            (generation / ".split.mk").touch()
            (generation / ".inuse").touch()
            (generation / "fixture.ld").write_text("SECTIONS { .text : { obj/asm/alpha.o(.text) } }\n")
            (self.root / "build" / version).symlink_to(generation.name)
        (self.root / "config.toml").write_text(toml.dumps(data))
        self.project = config.load(self.root)
        from unittest.mock import patch

        from tests.objdiff_fixture import install
        from tests.preprocessor import output
        from tests.process_fakes import boundary, copy
        from unbake.decomp import trial_compile
        from unbake.layout import entries
        from unbake.match import staging
        from unbake.project import build
        from unbake.typemap import declarations

        install(self)
        from tests.report_fixture import install as reports

        reports(self)
        for compiler in self.project.compilers.values():
            compiler.cc.parent.mkdir(parents=True, exist_ok=True)
            compiler.cc.write_bytes(b"fixture compiler")
            compiler.as_.write_bytes(b"fixture assembler")
            compiler.sha256.write_text("")

        def compiled(project, policy, source, version, destination):
            words = list(self.words)
            content = source.read_text()
            if "void tail" not in content:
                words = words[:3]
            elif "s->a = 1" in content:
                words[-1] = 0xA0800349
            text = assembly("alpha", words[:3])
            if len(words) > 3:
                text += assembly("tail", words[3:])
            destination.parent.mkdir(parents=True, exist_ok=True)
            return assemble(destination.parent, destination.stem, text)

        def merged(command, **kwargs):
            if "-r" not in command:
                return output(command, **kwargs)
            self.assertEqual(command[1], "-r")
            self.assertIn("-T", command)
            destination = Path(command[command.index("-o") + 1])
            objects = command[command.index("-o") + 2 :]
            from tests.elf_fixture import write_object
            from unbake.project_tools.elf import Object

            code, symbols = bytearray(), []
            for path in objects:
                obj = Object(path)
                offset = len(code)
                code.extend(obj.content(obj.section(".text")))
                symbols.extend(
                    (row["name"], ".text", offset + row["value"], row["size"], row["info"])
                    for table in obj.symbols.values()
                    for row in table
                    if row["section"] == obj.section(".text") and row["info"] >> 4
                )
            write_object(destination, {".text": bytes(code)}, symbols)
            return subprocess.CompletedProcess(command, 0, "", "")

        def proof(project, policy, versions, *, tree, generation_for):
            from unbake.match import staging
            from unbake.project_tools.extract import unit_ranges

            staged = staging.project_at(project, tree)
            results = {}
            for version in versions:
                generation = generation_for(version)
                generation.mkdir(parents=True, exist_ok=True)
                for source in staged.src.glob("*.c"):
                    compiled(staged, policy, source, version, generation / "obj/src" / (source.stem + ".o"))
                (generation / "unit-ranges.json").write_text(
                    json.dumps(unit_ranges(staged.version(version).split.read_text()))
                )
                (generation / ".inuse").touch()
                log = generation / "build.log"
                log.write_text("fixture proof")
                (generation / f"{project.name}.{version}.z64").write_bytes(
                    project.version(version).baserom.read_bytes()
                )
                results[version] = build.BuildResult(version, True, "fixture: OK", log, generation)
            return results

        for mock in (
            patch.object(build, "build", side_effect=proof),
            boundary(trial_compile, merged),
            patch.object(toolchain, "ensure"),
            patch.object(toolchain, "verify", return_value={}),
            patch.object(build, "compile_object", side_effect=compiled),
            patch.object(
                build, "compile_versions", side_effect=lambda project, policy, jobs, **_: {v: {} for v in jobs}
            ),
            boundary(entries, output),
            boundary(declarations, output),
            boundary(staging, copy),
        ):
            mock.start()
            self.addCleanup(mock.stop)
        for name, text in makefile.helpers(self.project).items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        self.source = self.directory / "alpha.c"
        self.text = (
            "struct State { char pad[0x344]; short counter; char gap[2]; unsigned char a, gap2, b; };\n"
            "void alpha(int *p) { p[0] = 0; p[1] = 0; }\n"
            "void tail(struct State *s) { s->counter = 0; s->b = 0; s->a = 0; }\n"
        )
        self.source.write_text(self.text)
        (self.root / "layout.toml").write_bytes(
            map.encoded(map.default(2, map.catalog(self.project), self.project.versions))
        )

    def run_try(self):
        script = Path(sysconfig.get_path("scripts")) / "unbake"
        environment = dict(os.environ, PYTHONNOUSERSITE="1")
        environment.pop("PYTHONPATH", None)
        before = set((self.directory / "scratch").glob("alpha-*/manifest.json"))
        result = cli_process(
            [
                str(script),
                "--project",
                str(self.root),
                "try",
                str(self.source),
                "--scratch",
                str(self.directory / "scratch"),
            ],
            cwd=self.directory,
            env=environment,
            capture_output=True,
            text=True,
            timeout=60,
        )
        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 0, output)
        self.assertEqual(sum(line.startswith("Next:") for line in output.splitlines()), 1, output)
        created = set((self.directory / "scratch").glob("alpha-*/manifest.json")) - before
        self.assertEqual(len(created), 1)
        manifest = json.loads(next(iter(created)).read_text())
        return output, manifest

    def test_merged_owner_accepts_two_definitions_and_rejects_tail_changes(self):
        original = {version: self.project.version(version).split.read_bytes() for version in self.project.versions}
        output, manifest = self.run_try()
        self.assertIn("identical 7 of 7", output)
        for version in self.project.versions:
            self.assertEqual(
                manifest["entries"][version]["entries"],
                [{"name": "alpha", "offset": 0}, {"name": "tail", "offset": 12}],
            )
            self.assertEqual(self.project.version(version).split.read_bytes(), original[version])
            self.assertEqual(split.functions(self.project, version)[0].entries, (("alpha", 0), ("tail", 12)))
        self.source.write_text(self.text.replace("s->a = 0", "s->a = 1"))
        output, _ = self.run_try()
        self.assertIn("owner fuzzy bar: FAIL", output)
        self.assertNotIn("identical 7 of 7", output)

    def test_missing_secondary_definition_cannot_match_only_the_first_symbol(self):
        self.source.write_text(self.text[: self.text.index("void tail")])
        output, _ = self.run_try()
        self.assertIn("entry tail: missing or moved", output)
        self.assertIn("owner fuzzy bar: FAIL", output)

    def test_separate_native_rows_are_one_item_without_boundary_edits(self):
        for version in self.project.versions:
            configured = self.project.version(version)
            configured.split.write_text(
                configured.split.read_text().replace("  - [0x5C, bin,", "      - [0x4C, asm, tail]\n  - [0x5C, bin,")
            )
            generation = self.project.build_link(version).resolve()
            objects = generation / "obj/asm"
            assemble(objects, "alpha", assembly("alpha", self.words[:3]))
            assemble(objects, "tail", assembly("tail", self.words[3:]))
            (generation / "fixture.ld").write_text(
                "SECTIONS { .text : { obj/asm/alpha.o(.text) obj/asm/tail.o(.text) } }\n"
            )
        map.edit_members(self.project, {"alpha": ("alpha", "tail")})
        for name, text in makefile.render(self.project).items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        script = Path(sysconfig.get_path("scripts")) / "unbake"
        environment = dict(os.environ, PYTHONNOUSERSITE="1")
        environment.pop("PYTHONPATH", None)
        mapped = cli_process(
            [str(script), "--project", str(self.root), "map"],
            cwd=self.directory,
            env=environment,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(mapped.returncode, 0, mapped.stdout + mapped.stderr)
        output, manifest = self.run_try()
        self.assertIn("identical 7 of 7", output)
        for version in self.project.versions:
            self.assertEqual(manifest["entries"][version]["rows"], ["alpha", "tail"])
            self.assertEqual(len(split.functions(self.project, version)), 2)
        script = Path(sysconfig.get_path("scripts")) / "unbake"
        environment = dict(os.environ, PYTHONNOUSERSITE="1")
        environment.pop("PYTHONPATH", None)
        result = cli_process(
            [str(script), "--project", str(self.root), "submit", str(self.source)],
            cwd=self.directory,
            env=environment,
            capture_output=True,
            text=True,
            timeout=120,
        )
        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 0, output)
        self.assertIn("matched on VERSION us, us-rev1", output)
        for version in self.project.versions:
            owners = split.functions(self.project, version)
            self.assertEqual(len(owners), 1)
            self.assertEqual(owners[0].entries, (("alpha", 0), ("tail", 12)))
            self.assertEqual(owners[0].kind, "c")
            self.assertIn(f"OK(submit): {version}:", output)

    def test_moved_entry_fails_even_when_every_instruction_is_identical(self):
        from unbake.project_tools.elf import Object

        for version in self.project.versions:
            target = self.project.build_link(version) / "obj/asm/alpha.o"
            obj = Object(target)
            for index, table in obj.symbols.items():
                for ordinal, symbol in enumerate(table):
                    if symbol["name"] == "tail":
                        struct.pack_into(">I", obj.data, obj.sections[index][4] + ordinal * 16 + 4, 8)
            target.write_bytes(obj.data)
        output, _ = self.run_try()
        self.assertIn("identical 7 of 7", output)
        self.assertIn("entry tail: missing or moved from +0x8", output)
        self.assertIn("owner fuzzy bar: FAIL", output)

    def test_one_versions_tail_difference_prevents_cross_version_exactness(self):
        version = self.project.versions[-1]
        target = self.project.build_link(version) / "obj/asm/alpha.o"
        assemble(
            target.parent,
            "alpha",
            assembly("alpha", self.words[:3]) + assembly("tail", [*self.words[3:-1], 0xA0800349]),
        )
        output, manifest = self.run_try()
        self.assertIn("VERSION us: identical 7 of 7", output)
        self.assertIn(f"VERSION {version}: identical 6 of 7", output)
        self.assertIn("owner fuzzy bar: FAIL", output)
        self.assertEqual(manifest["versions"], list(self.project.versions))

    def test_inferred_fragment_can_be_covered_only_by_identical_owner_bytes(self):
        from unbake.project_tools.elf import Object

        self.source.write_text("void alpha(int *p) { p[0] = 0; p[1] = 0; }\n")
        for version in self.project.versions:
            configured = self.project.version(version)
            configured.symbols.write_text("alpha = 0x80001000; // type:func\n")
            configured.split.write_text(
                configured.split.read_text().replace("  - [0x5C, bin, trailer]", "  - [0x4C, bin, trailer]")
            )
            target = self.project.build_link(version) / "obj/asm/alpha.o"
            # The disassembler split a compiled return/delay-slot fragment out
            # of the native owner. It is not a committed external entry.
            assemble(target.parent, "alpha", assembly("alpha", self.words[:1]) + assembly("fragment", self.words[1:3]))
        output, _ = self.run_try()
        self.assertIn("identical 3 of 3", output)
        self.assertIn("owner fuzzy bar: PASS", output)
        target = self.project.build_link(self.project.versions[-1]) / "obj/asm/alpha.o"
        obj = Object(target)
        section = obj.section(".text")
        struct.pack_into(">I", obj.data, obj.sections[section][4] + 8, 0xAC800008)
        target.write_bytes(obj.data)
        output, _ = self.run_try()
        self.assertIn("owner fuzzy bar: FAIL", output)

    def test_entry_size_difference_is_not_waived_by_equal_interval_bytes(self):
        from unbake.project_tools.elf import Object

        for version in self.project.versions:
            target = self.project.build_link(version) / "obj/asm/alpha.o"
            obj = Object(target)
            for index, table in obj.symbols.items():
                for ordinal, symbol in enumerate(table):
                    if symbol["name"] == "tail":
                        struct.pack_into(">I", obj.data, obj.sections[index][4] + ordinal * 16 + 8, 8)
            target.write_bytes(obj.data)
        output, _ = self.run_try()
        self.assertIn("identical 7 of 7", output)
        self.assertIn("entry tail: compiled symbol size differs", output)
        self.assertIn("owner fuzzy bar: FAIL", output)

    def test_nonzero_text_after_the_last_compiled_symbol_is_compared(self):
        from unbake.decomp.score import diff
        from unbake.decomp.trial import comparison_identical
        from unbake.decomp.trial_compare import compare_object
        from unbake.decomp.trial_entries import comparison_views

        candidate = assemble(
            self.directory,
            "candidate",
            assembly("alpha", self.words[:3]) + assembly("tail", self.words[3:]) + ".word 0x24020001\n",
        )
        before = candidate.read_bytes()
        target = self.project.build_link("us") / "obj/asm/alpha.o"
        left, right, failures = comparison_views(
            self.project, self.policy, self.source, "us", target, candidate, self.directory
        )
        document = diff(self.policy, "us", "alpha", left, right, self.directory / "diff.json")
        comparison = compare_object("us", document, "alpha")
        self.assertEqual(failures, [])
        self.assertFalse(comparison_identical(comparison))
        self.assertEqual(comparison.typed["inserted"], 1)
        self.assertEqual(candidate.read_bytes(), before)
