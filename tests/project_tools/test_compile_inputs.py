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
from unbake.project_tools.compile_identity import driver_content, driver_names, selected_pins


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

    def test_driver_logic_ignores_comments_and_link_methods_but_binds_codegen(self):
        project, _ = fixture(self.root, case=self)
        write_rendered(project)
        for name, change, changed in (
            ("codegen.py", lambda text: text + "\n# cache reporting change\n", False),
            ("codegen.py", lambda text: text.replace("Byte-producing compile logic", "Updated documentation"), False),
            (
                "codegen.py",
                lambda text: text.replace(
                    "source_name = Path(args.unit).stem", 'source_name = "changed" + Path(args.unit).stem'
                ),
                True,
            ),
            ("elf.py", lambda text: text.replace("result = []", "result = [123]"), False),
            ("elf.py", lambda text: text.replace('self.section(".text")', 'self.section(".data")'), True),
        ):
            with self.subTest(name=name, changed=changed):
                driver = project.tools / name
                original = driver.read_text()
                expected = driver_content(driver)
                driver.write_text(change(original))
                self.assertEqual(driver_content(driver) != expected, changed)
                driver.write_text(original)
        for kind, sn64, expected in (
            ("cc", False, ("codegen.py", "elf.py")),
            ("as", False, ("codegen.py",)),
            ("cc", True, ("codegen.py", "elf.py", "sn64_cc.py")),
            ("as", True, ("codegen.py", "sn64_cc.py", "resolve_external_branches.py")),
        ):
            with self.subTest(kind=kind, sn64=sn64):
                self.assertEqual(driver_names(kind, sn64), expected)

    def test_selected_pins_exclude_helpers_and_other_compilers(self):
        groups = {
            "tools": {"tools/cc": "a", "tools/cache.py": "b", "tools/pool_slices.py": "c"},
            "tools/ido": {"tools/ido/cc": "d"},
            "tools/ido/lib": {"tools/ido/lib/cc1": "e"},
            "tools/other": {"tools/other/cc": "f"},
        }
        for compiler, expected in (
            ("tools/cc", {"tools/cc": "a"}),
            ("tools/ido/cc", {"tools/ido/cc": "d", "tools/ido/lib/cc1": "e"}),
            ("tools/missing", {}),
        ):
            with self.subTest(compiler=compiler):
                self.assertEqual(selected_pins(groups, Path(compiler), Path("tools")), expected)

    def test_ido_pins_exclude_other_languages_linkers_and_catalogs(self):
        names = ("cc", "cfe", "uopt", "acpp", "upas", "edgcpfe", "c++filt", "ld", "crt1.o", "err.english.cc")
        groups = {"tools/ido": {"tools/ido/" + name: name for name in names}}
        self.assertEqual(
            set(selected_pins(groups, Path("tools/ido/cc"), Path("tools"), "ido")),
            {"tools/ido/" + name for name in ("cc", "cfe", "uopt", "acpp")},
        )
        direct = {"tools": {"tools/cc": "compiler", "tools/cache.py": "service"}}
        self.assertEqual(selected_pins(direct, Path("tools/cc"), Path("tools").absolute()), {"tools/cc": "compiler"})

    def test_object_key_tracks_only_byte_inputs(self):
        self.addCleanup(os.chdir, Path.cwd())
        os.chdir(self.root)
        project, _ = fixture(self.root, case=self)
        write_rendered(project)
        source = project.src / "middle.c"
        artifact = self.root / "cached.o"
        artifact.write_bytes(b"object")
        args = argparse.Namespace(
            recipe=project.tools / "build.json",
            source=source,
            output=self.root / "result.o",
            kind="cc",
            version="us",
            unit="src/middle.c",
            non_matching="0",
            depfile=None,
            dep_target=None,
            cache_root=self.root / "cache",
        )
        original = json.loads(args.recipe.read_text())
        compiler_path = self.root / original["compilers"][original["default_compiler"]]["cc"]
        compiler_content = compiler_path.read_bytes()
        content = b"int same;"
        with (
            patch.object(compile, "run", side_effect=lambda *args, **kwargs: content),
            patch.object(compile.Cache, "produce", return_value=artifact) as cached,
        ):
            compile.compile_object(args, original.copy())
            baseline = cached.call_args.args[1]
            cases = (
                ("link helper", False),
                ("cache service", False),
                ("driver service", False),
                ("manifest helper", False),
                ("other compiler", False),
                ("other unit flags", False),
                ("codegen flags", True),
                ("compiler binary", True),
                ("preprocessed closure", True),
            )
            for change, changed in cases:
                with self.subTest(change=change):
                    data = json.loads(json.dumps(original))
                    target = None
                    previous = None
                    if change in {"link helper", "cache service", "driver service"}:
                        target = (
                            project.tools
                            / {
                                "link helper": "pool_slices.py",
                                "cache service": "cache.py",
                                "driver service": "compile.py",
                            }[change]
                        )
                        previous = target.read_bytes()
                        target.write_bytes(previous + b"\n# changed service\n")
                    elif change == "manifest helper":
                        target = project.tools / "compiler.sha256"
                        previous = target.read_bytes()
                        target.write_bytes(previous + b"0" * 64 + b"  tools/cache.py\n")
                    elif change == "other compiler":
                        data["compilers"]["unselected"] = dict(kind="ido", cc="missing", cflags=["-O0"], **{"as": "as"})
                    elif change == "other unit flags":
                        data["unit_cflags"]["elsewhere"] = ["-O0"]
                    elif change == "codegen flags":
                        data["compilers"][data["default_compiler"]]["cflags"].append("-O1")
                    elif change == "compiler binary":
                        target, previous = compiler_path, compiler_content
                        target.write_bytes(previous + b"\n# new binary\n")
                    elif change == "preprocessed closure":
                        content = b"int changed;"
                    compile.tool_digest.cache_clear()
                    compile.compile_object(args, data)
                    self.assertEqual(cached.call_args.args[1] != baseline, changed)
                    if target is not None:
                        target.write_bytes(previous)
                    content = b"int same;"

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
                    patch.object(compile, "file_signature", return_value=(0, 0, 0, 0, 0)),
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
                assembler_flags, assembler_inputs = compile.assembly_inputs(
                    [
                        *(data["sn64_asflags"] if kind == "sn64" else data["asflags"]),
                        "-I" + str(Path(data["asm"]) / "us/include"),
                    ]
                )
                expected_digest = key(
                    content,
                    "middle.i",
                    json.dumps([generation, assembler_flags if kind == "sn64" else []], sort_keys=True),
                    "unchanged-tools",
                    *(assembler_inputs if kind == "sn64" else []),
                )
                self.assertEqual(cached.call_args.args[1], expected_digest)
