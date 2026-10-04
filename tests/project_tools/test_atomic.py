"""Hardlinked warmed outputs survive every build publication and failure path."""

import argparse
import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.helper_fixture import extraction
from tests.project.makefile_fixture import fixture, write_rendered
from unbake.project_tools import atomic, compile, extract
from unbake.project_tools.elf import Object


class AtomicTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.addCleanup(patch.stopall)
        self.root = Path(temporary.name).resolve()
        self.addCleanup(os.chdir, Path.cwd())
        os.chdir(self.root)

    def shared(self, path, content=b"old bytes"):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        source = path.with_name(path.name + ".source")
        source.unlink(missing_ok=True)
        os.link(path, source)
        state = source.stat()
        return source, (state.st_ino, state.st_mtime_ns, source.read_bytes())

    def unchanged(self, record):
        source, expected = record
        state = source.stat()
        self.assertEqual((state.st_ino, state.st_mtime_ns, source.read_bytes()), expected)

    def test_write_receipt_and_stale_partial_never_modify_source(self):
        path = self.root / "object.o"
        original = self.shared(path)
        stale = self.shared(path.with_name("object.o.partial"))
        atomic.write(path, b"new bytes")
        self.unchanged(original)
        self.unchanged(stale)
        self.assertNotEqual(path.stat().st_ino, original[0].stat().st_ino)
        original[0].unlink()
        receipt = self.shared(path)
        atomic.receipt(path)
        self.unchanged(receipt)
        self.assertEqual(path.read_bytes(), b"old bytes")
        missing = self.root / "absent.built"
        atomic.receipt(missing)
        self.assertEqual(missing.read_bytes(), b"")
        self.assertFalse(list(self.root.glob(".publish-*")))

    def test_write_and_rename_failures_preserve_destination_and_cleanup(self):
        path = self.root / "object.o"
        original = self.shared(path)
        for operation in ("fsync", "replace"):
            with self.subTest(operation=operation):
                with patch.object(atomic.os, operation, side_effect=OSError("injected")), self.assertRaises(OSError):
                    atomic.write(path, b"new")
                self.unchanged(original)
                self.assertFalse(list(self.root.glob(".publish-*")))

    def test_text_append_copy_and_tree_replace_shared_files(self):
        source = self.root / "input"
        source.write_bytes(b"copied")
        tree = self.root / "input-tree"
        tree.mkdir()
        (tree / "record").write_bytes(b"tree")
        for operation in ("text", "append", "copyfile", "copy2", "copy", "copytree"):
            with self.subTest(operation=operation):
                path = self.root / operation / "record"
                original = self.shared(path)
                path.chmod(0o640)
                if operation == "text":
                    atomic.text(path, "new text", encoding="utf-8")
                    expected = b"new text"
                elif operation == "append":
                    with atomic.stream(path, "a", encoding="utf-8") as output:
                        output.write(" appended")
                    expected = b"old bytes appended"
                elif operation == "copytree":
                    atomic.copytree(tree, path.parent, dirs_exist_ok=True)
                    expected = b"tree"
                else:
                    getattr(atomic, operation)(source, path)
                    expected = b"copied"
                self.unchanged(original)
                self.assertEqual(path.read_bytes(), expected)
                self.assertNotEqual(path.stat().st_ino, original[0].stat().st_ino)
                if operation in {"text", "append"}:
                    self.assertEqual(path.stat().st_mode & 0o777, 0o640)

    def test_tree_cannot_reintroduce_a_direct_copy_callback(self):
        with self.assertRaisesRegex(ValueError, "atomic publication"):
            atomic.copytree(self.root / "source", self.root / "destination", copy_function=atomic.shutil.copy2)

    def test_fsync_precedes_replace_in_the_same_directory(self):
        path = self.root / "database.json"
        original = self.shared(path)
        events = []
        replace = os.replace

        def sync(descriptor):
            self.assertNotEqual(os.fstat(descriptor).st_ino, original[0].stat().st_ino)
            events.append("sync")

        def publish(source, destination):
            self.assertEqual(Path(source).parent, Path(destination).parent)
            self.assertEqual(events[-1], "sync")
            events.append("replace")
            replace(source, destination)

        with (
            patch.object(atomic.os, "fsync", side_effect=sync),
            patch.object(atomic.os, "replace", side_effect=publish),
        ):
            atomic.write(path, b"new", mode=0o600)
        self.unchanged(original)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(events[-1], "replace")

    def test_failed_stream_preserves_destination_and_append_keeps_all_records(self):
        from concurrent.futures import ThreadPoolExecutor

        path = self.root / "events.jsonl"
        original = self.shared(path, b"")
        with self.assertRaisesRegex(ValueError, "injected"), atomic.stream(path) as output:
            output.write("partial")
            raise ValueError("injected")
        self.unchanged(original)
        self.assertEqual(path.read_bytes(), b"")

        def append(index):
            with atomic.stream(path, "a") as output:
                output.write(str(index) + "\n")

        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(append, range(20)))
        self.assertEqual(sorted(map(int, path.read_text().splitlines())), list(range(20)))
        self.unchanged(original)
        self.assertFalse(list(self.root.glob(".publish-*")))

    def test_commands_publish_elf_map_assets_and_rom_only_after_success(self):
        for names in (("game.elf", "game.map"), ("asset.o",), ("game.z64",)):
            with self.subTest(names=names):
                paths = [self.root / name for name in names]
                originals = [self.shared(path) for path in paths]

                def run(argv, paths=paths, originals=originals, **kwargs):
                    self.assertEqual(kwargs, {"check": True})
                    for index, path in enumerate(paths):
                        self.assertNotIn(str(path), argv)
                        Path(argv[index + 1]).write_bytes(b"new")
                        self.unchanged(originals[index])

                with patch.object(atomic.subprocess, "run", side_effect=run):
                    atomic.command(paths, ["tool", *map(str, paths)])
                for path, original in zip(paths, originals, strict=True):
                    self.unchanged(original)
                    self.assertEqual(path.read_bytes(), b"new")
                    self.assertNotEqual(path.stat().st_ino, original[0].stat().st_ino)

    def test_command_failure_missing_output_and_invalid_arguments_do_not_publish(self):
        path = self.root / "game.elf"
        original = self.shared(path)

        def fail(argv, **kwargs):
            Path(argv[-1]).write_bytes(b"broken partial output")
            raise subprocess.CalledProcessError(1, argv)

        with patch.object(atomic.subprocess, "run", side_effect=fail), self.assertRaises(subprocess.CalledProcessError):
            atomic.command([path], ["tool", str(path)])
        with patch.object(atomic.subprocess, "run"), self.assertRaisesRegex(ValueError, "every declared"):
            atomic.command([path], ["tool", str(path)])
        for outputs, argv in (([], ["tool"]), ([path, path], ["tool", str(path)]), ([path], ["tool"])):
            with self.assertRaises(ValueError):
                atomic.command(outputs, argv)
        self.unchanged(original)
        self.assertFalse(list(self.root.glob(".publish-*")))

    def test_compile_depfiles_objects_and_inputs_for_all_compilers_and_cache_states(self):
        project, _ = fixture(self.root, case=self)
        write_rendered(project)
        source = project.src / "middle.c"
        header = project.include[0] / "value.h"
        for kind, assembly in (("ido", False), ("gcc", False), ("sn64", False), ("gcc", True), ("sn64", True)):
            for cached in (False, True):
                with self.subTest(kind=kind, assembly=assembly, cached=cached):
                    data = json.loads((project.tools / "build.json").read_text())
                    ident = data["default_compiler"]
                    data["compilers"][ident]["kind"] = kind
                    data["compilers"][ident]["as"] = str(project.tools / "as")
                    data["assembly_compiler"] = ident if kind == "sn64" else None
                    data["as"] = str(project.tools / "as")
                    args = argparse.Namespace(
                        recipe=project.tools / "build.json",
                        source=source,
                        output=self.root / "result.o",
                        kind="as" if assembly else "cc",
                        version="us",
                        unit="src/middle.c",
                        non_matching="0",
                        depfile=self.root / "result.d",
                        dep_target=None,
                        cache_root=self.root / "cache",
                        symbols=None,
                    )
                    originals = [
                        self.shared(args.output),
                        self.shared(args.depfile),
                        self.shared(args.output.with_suffix(".inputs.json")),
                    ]
                    artifact = self.root / "cache-output"
                    artifact.write_bytes(b"new object")

                    def run(argv, args=args, assembly=assembly, **kwargs):
                        for flag in ("-MF", "--MD"):
                            if flag in argv:
                                dependency = Path(argv[argv.index(flag) + 1])
                                self.assertNotEqual(dependency, args.depfile)
                                target = argv[argv.index("-MT") + 1] if "-MT" in argv else "old.o"
                                dependency.write_text(f"{target}: {source} {header}\n")
                        if "-o" in argv:
                            self.assertEqual(
                                argv[-3] if argv[-2] == "-o" else argv[-1], "middle.s" if assembly else "middle.i"
                            )
                            work = kwargs["cwd"]
                            self.assertTrue((work / ("middle.s" if assembly else "middle.i")).is_file())
                            self.assertNotIn(str(work), argv[-3] if argv[-2] == "-o" else argv[-1])
                            self.assertNotEqual(work, args.output.parent)
                            Path(argv[argv.index("-o") + 1]).write_bytes(b"new object")
                        return f'# 1 "{source}"\n# 1 "{header}"\nint value;\n'.encode()

                    def produce(cache_kind, digest, writer, cached=cached, artifact=artifact):
                        if not cached:
                            writer(artifact)
                        return artifact

                    def assemble(text, destination, assembler, flags, **kwargs):
                        destination.write_bytes(b"new object")

                    with (
                        patch.object(compile, "run", side_effect=run),
                        patch.object(compile, "resolve_tool", side_effect=lambda value: value),
                        patch.object(compile, "tool_digest", return_value="tools"),
                        patch.object(compile, "file_signature", return_value=(0, 0, 0, 0, 0)),
                        patch.object(compile.Cache, "produce", side_effect=produce),
                        patch("unbake.project_tools.elf.Object"),
                        patch("abumasn64.assemble.assemble", side_effect=assemble),
                        patch("unbake.project_tools.resolve_external_branches.read_symbols", return_value=({}, {})),
                        patch(
                            "unbake.project_tools.resolve_external_branches.resolve", side_effect=lambda text, *a: text
                        ),
                    ):
                        compile.compile_object(args, data)
                    for original in originals:
                        self.unchanged(original)
                    self.assertEqual(args.output.read_bytes(), b"new object")
                    self.assertTrue(args.depfile.read_text().startswith(str(args.output.resolve()) + ":"))
                    if not assembly:
                        self.assertEqual(
                            json.loads(args.output.with_suffix(".inputs.json").read_text()),
                            {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in (source, header)},
                        )
                    self.assertFalse(list(self.root.glob(".publish-*")))
                    for original in originals:
                        original[0].unlink()

    def test_compile_failure_keeps_original_depfile_and_args(self):
        depfile = self.root / "result.d"
        original = self.shared(depfile)
        args = argparse.Namespace(depfile=depfile, dep_target=None, output=self.root / "result.o")

        def fail(item, data):
            item.depfile.write_bytes(b"failed dependencies")
            raise ValueError("failed compiler")

        with patch.object(compile, "_compile_object", side_effect=fail), self.assertRaises(ValueError):
            compile.compile_object(args)
        self.unchanged(original)
        self.assertEqual(args.depfile, depfile)
        self.assertFalse(list(self.root.glob(".publish-*")))

    def test_batch_receipts_and_cancel_marker_are_atomic(self):
        args = argparse.Namespace(
            batch=[self.root / "src/one.c"],
            source=self.root / "src",
            output=self.root / "obj",
            kind="cc",
            recipe=self.root / "recipe",
            cancel_file=None,
        )
        receipt = self.shared(self.root / "obj/one.built")
        with patch.object(compile, "read_recipe", return_value={}), patch.object(compile, "compile_object"):
            compile.compile_batch(args)
        self.unchanged(receipt)
        marker = self.root / "stop"
        with (
            patch.object(compile, "read_recipe", return_value={}),
            patch.object(compile, "compile_object", side_effect=ValueError("failed")),
        ):
            args.cancel_file = marker
            with self.assertRaisesRegex(ValueError, "failed"):
                compile.compile_batch(args)
        self.assertEqual(marker.read_bytes(), b"")

    def test_extraction_refresh_restore_and_recompute_preserve_all_shared_files(self):
        project, _ = fixture(self.root, case=self)
        write_rendered(project)
        result = extraction(project)
        self.assertEqual(result.returncode, 0, result.stderr)
        build = project.build_link("us")
        originals = []
        for path in list(build.rglob("*")):
            if path.is_file():
                originals.append(self.shared(path, path.read_bytes()))
        for mode in ("refresh", "restore", "recompute"):
            with self.subTest(mode=mode):
                if mode != "refresh":
                    (build / ".extract-key").unlink(missing_ok=True)
                with (
                    patch.object(extract.Cache, "get", return_value=None)
                    if mode == "recompute"
                    else patch.object(extract, "_WRITTEN", {})
                ):
                    result = extraction(project)
                self.assertEqual(result.returncode, 0, result.stderr)
                for original in originals:
                    self.unchanged(original)
        self.assertFalse(list(build.glob(".publish-*")))

    def test_elf_trim_replaces_the_object_inode(self):
        from tests.decomp.support import assemble

        path = assemble(
            self.root, "trim", ".text\n.globl trim\n.type trim,@function\ntrim:\n.word 1\n.size trim,.-trim\n.space 8\n"
        )
        original = self.shared(path, path.read_bytes())
        Object(path).trim_text()
        self.unchanged(original)
        self.assertNotEqual(path.stat().st_ino, original[0].stat().st_ino)

    def test_run_passes_isolated_cwd_and_preserves_tool_failure(self):
        for directory in (None, self.root):
            with self.subTest(cwd=directory):
                kwargs = {"capture_output": True}
                if directory is not None:
                    kwargs["cwd"] = directory
                with patch.object(
                    compile.subprocess, "run", return_value=subprocess.CompletedProcess(["cc"], 0, b"output", b"")
                ) as run:
                    self.assertEqual(compile.run(["cc"], cwd=directory), b"output")
                    run.assert_called_once_with(["cc"], **kwargs)
        # A failure reports stderr; stdout (often preprocessed text) only when stderr is empty.
        for stdout, stderr, message in ((b"out", b"err", "cc exited 1: err$"), (b"out", b"", "cc exited 1: out$")):
            with (
                self.subTest(stderr=stderr),
                patch.object(
                    compile.subprocess, "run", return_value=subprocess.CompletedProcess(["cc"], 1, stdout, stderr)
                ),
                self.assertRaisesRegex(ValueError, message),
            ):
                compile.run(["cc"], cwd=self.root)
