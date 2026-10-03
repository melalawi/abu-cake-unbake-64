"""Shared compile inputs retain exact keys and observe edits between objects."""

import argparse
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.project.makefile_fixture import fixture, write_rendered
from unbake.project.cache import key
from unbake.project_tools import compile
from unbake.project_tools.compile_identity import LEGACY_DRIVER, OPTIMIZED_SHA256


class CompileInputTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.addCleanup(patch.stopall)
        self.root = Path(temporary.name).resolve()
        compile.dependency_digest.cache_clear()
        compile.manifest_pins.cache_clear()
        compile.tool_digest.cache_clear()

    def test_shared_header_reuses_digest_and_observes_same_size_edits_and_replacement(self):
        header = self.root / "shared.h"
        header.write_bytes(b"old content")
        stamp = header.stat().st_mtime_ns
        expected = {text: hashlib.sha256(text).hexdigest() for text in (b"old content", b"new content", b"replacement")}
        with (
            patch.object(Path, "read_bytes", autospec=True, side_effect=Path.read_bytes) as read,
            patch.object(compile.hashlib, "sha256", wraps=hashlib.sha256) as hash_header,
        ):
            self.assertEqual(compile.dependency_hash(str(header)), expected[b"old content"])
            self.assertEqual(compile.dependency_hash(str(header)), expected[b"old content"])
            self.assertEqual(read.call_count, 1)
            self.assertEqual(hash_header.call_count, 1)
            header.write_bytes(b"new content")
            os.utime(header, ns=(stamp, stamp))
            self.assertEqual(compile.dependency_hash(str(header)), expected[b"new content"])
            replacement = self.root / "replacement"
            replacement.write_bytes(b"replacement")
            os.utime(replacement, ns=(stamp, stamp))
            replacement.replace(header)
            self.assertEqual(compile.dependency_hash(str(header)), expected[b"replacement"])
            self.assertEqual(read.call_count, 3)
            self.assertEqual(hash_header.call_count, 3)
        header.unlink()
        with self.assertRaises(FileNotFoundError):
            compile.dependency_hash(str(header))

    def test_manifest_grouping_preserves_selection_and_observes_edits(self):
        manifest = self.root / "compiler.sha256"
        content = "# ignored\nmalformed\n111 *tools/cc/cc1\n222 tools/cc/cc1\n333 tools/other/as\n"
        manifest.write_text(content)
        with patch.object(Path, "read_text", autospec=True, side_effect=Path.read_text) as read:
            pins = compile.manifest_pins(manifest, compile.file_signature(manifest))
            self.assertEqual(pins, {"tools/cc": {"tools/cc/cc1": "222"}, "tools/other": {"tools/other/as": "333"}})
            self.assertEqual(compile.manifest_pins(manifest, compile.file_signature(manifest)), pins)
            self.assertEqual(read.call_count, 1)
            manifest.write_text(content.replace("222", "444"))
            updated = compile.manifest_pins(manifest, compile.file_signature(manifest))
            self.assertEqual(updated["tools/cc"], {"tools/cc/cc1": "444"})
            self.assertEqual(read.call_count, 2)

    def test_generated_driver_preserves_legacy_key_but_later_driver_edits_invalidate(self):
        project, _ = fixture(self.root, case=self)
        write_rendered(project)
        driver = project.tools / "compile.py"
        elf = project.tools / "elf.py"
        self.assertNotEqual(hashlib.sha256(driver.read_bytes()).hexdigest(), OPTIMIZED_SHA256)
        expected = key(LEGACY_DRIVER.encode(), elf)
        self.assertEqual(compile.tool_digest((driver, elf)), key(driver, elf))
        self.assertNotEqual(compile.tool_digest((driver, elf)), expected)
        driver.write_text(driver.read_text() + "\n# subsequent driver revision\n")
        compile.tool_digest.cache_clear()
        self.assertEqual(compile.tool_digest((driver, elf)), key(driver, elf))
        self.assertNotEqual(compile.tool_digest((driver, elf)), expected)

    def test_one_preprocessing_pass_preserves_dependency_evidence_for_each_compiler(self):
        project, _ = fixture(self.root, case=self)
        write_rendered(project)
        source = project.src / "middle.c"
        header = project.include[0] / "value.h"
        empty = project.include[0] / "empty.h"
        empty.write_bytes(b"")
        content = (
            f'# 1 "{source}"\n# 1 "<built-in>"\n# 1 "{header}"\n'
            f'# 1 "{empty}"\n# 2 "{source}"\n# 1 "{header}"\nint middle;\n'
        ).encode()
        expected_paths = [str(source), str(header), str(empty)]
        self.assertEqual(compile.preprocessed_dependencies(content, source), expected_paths)
        artifact = self.root / "cached.o"
        artifact.write_bytes(b"cached object")
        for kind, flags, count in (("ido", [], 1), ("gcc", [], 1), ("sn64", [], 1), ("ido", ["-P"], 2)):
            with self.subTest(kind=kind, flags=flags):
                data = json.loads((project.tools / "build.json").read_text())
                compiler = data["compilers"][data["default_compiler"]]
                compiler["kind"] = kind
                compiler["cflags"] = ["-O2", *flags]
                data["cpp"] = "cpp"
                data["cppflags"] = []
                args = argparse.Namespace(
                    recipe=project.tools / "build.json",
                    source=source,
                    output=self.root / "result.o",
                    kind="cc",
                    version="us",
                    unit="src/middle.c",
                    non_matching="0",
                    depfile=self.root / "result.d",
                    dep_target="custom-target",
                    cache_root=self.root / "cache",
                )
                commands = []

                def preprocess(command, commands=commands):
                    commands.append(command)
                    target = command[command.index("-MT") + 1] if "-MT" in command else "old.o"
                    rule = target + ": " + " ".join(expected_paths) + "\n"
                    if "-M" in command:
                        return rule.encode()
                    if "-MF" in command:
                        Path(command[command.index("-MF") + 1]).write_text(rule)
                    return content

                with (
                    patch.object(compile, "run", side_effect=preprocess),
                    patch.object(compile, "resolve_tool", side_effect=lambda value: value),
                    patch.object(compile, "tool_digest", return_value="unchanged-tools"),
                    patch.object(compile.Cache, "produce", return_value=artifact) as cached,
                ):
                    compile.compile_object(args, data)
                self.assertEqual(len(commands), count)
                from unbake.typemap.split import consumer_macro

                self.assertNotIn("-D" + consumer_macro(source.stem) + "=1", commands[0])
                self.assertEqual(args.depfile.read_text(), "custom-target: " + " ".join(expected_paths) + "\n")
                self.assertEqual(
                    json.loads(args.output.with_suffix(".inputs.json").read_text()),
                    {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in (source, header, empty)},
                )
                self.assertEqual(args.output.read_bytes(), artifact.read_bytes())
                # Dependency collection must not rewrite the preprocessed cache input.
                generation = ["-O2", *flags]
                manifest = project.tools / "compiler.sha256"
                selected = (
                    compile.manifest_pins(manifest, compile.file_signature(manifest)).get(
                        str(Path(compiler["cc"]).parent), {}
                    )
                    if manifest.is_file()
                    else {}
                )
                assembler_flags, assembler_inputs = compile.assembly_inputs(
                    [
                        *(data["sn64_asflags"] if kind == "sn64" else data["asflags"]),
                        "-I" + str(Path(data["asm"]) / "us/include"),
                    ]
                )
                expected_digest = key(
                    content,
                    "middle.i",
                    json.dumps([selected, generation, assembler_flags], sort_keys=True),
                    "unchanged-tools",
                    *assembler_inputs,
                )
                self.assertEqual(cached.call_args.args[1], expected_digest)
