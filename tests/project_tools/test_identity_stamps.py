"""Content identities isolate refresh, compiler, kind and publication changes."""

import argparse
import copy
import json
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tests.project.makefile_fixture import fixture, write_rendered
from unbake.project import makefile, setup
from unbake.project_tools import compile
from unbake.project_tools import compile_identity as identity


class IdentityStampTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.addCleanup(patch.stopall)
        self.root = Path(temporary.name).resolve()
        self.addCleanup(os.chdir, Path.cwd())
        os.chdir(self.root)
        compile.tool_digest.cache_clear()

    def test_stamp_publication_table_and_hardlinks(self):
        stamp = self.root / "stamp"
        identity.publish_stamp(stamp, "same")
        sibling = self.root / "sibling"
        sibling.hardlink_to(stamp)
        os.utime(stamp, ns=(100, 100))
        for value, changed in (("same", False), ("different", True), ("different", False)):
            with self.subTest(value=value, changed=changed):
                before = stamp.stat()
                with patch.object(identity, "write", wraps=identity.write) as write:
                    identity.publish_stamp(stamp, value)
                self.assertEqual(write.call_count, int(changed))
                if not changed:
                    self.assertEqual(stamp.stat().st_mtime_ns, before.st_mtime_ns)
                    self.assertEqual(stamp.stat().st_ino, before.st_ino)
                self.assertEqual(stamp.read_text(), value + "\n")
                self.assertEqual(sibling.read_text(), "same\n")
        stamp.unlink()
        identity.publish_stamp(stamp, "same")
        self.assertEqual(stamp.read_text(), "same\n")

    def test_concurrent_equal_stamp_publication_writes_once(self):
        stamp = self.root / "concurrent/stamp"
        with patch.object(identity, "write", wraps=identity.write) as write:
            with ThreadPoolExecutor(max_workers=4) as pool:
                list(pool.map(lambda _: identity.publish_stamp(stamp, "same"), range(16)))
            self.assertEqual(write.call_count, 1)
        self.assertEqual(stamp.read_text(), "same\n")

    def test_failed_refresh_keeps_unchanged_stamp_timestamps_and_graph(self):
        project, _ = fixture(self.root, case=self)
        write_rendered(project)
        graph = project.root / "Makefile"
        graph.write_text("previous graph\n")
        stamps = {path: path.stat().st_mtime_ns for path in (project.tools / "compile").rglob("*") if path.is_file()}
        original = setup.compiler_files.atomic_bytes

        def fail_manifest(path, content):
            if path.name == "compiler.sha256":
                raise OSError("publication failed")
            original(path, content)

        # Ensure a pin changes, so failure follows the graph publication.
        helper = project.tools / "compile.py"
        helper.write_text("old helper\n")
        with (
            patch.object(setup.compiler_files, "atomic_bytes", side_effect=fail_manifest),
            self.assertRaisesRegex(OSError, "publication failed"),
        ):
            setup.refresh_helpers(project)
        self.assertEqual(graph.read_text(), "previous graph\n")
        self.assertEqual(helper.read_text(), "old helper\n")
        self.assertEqual({path: path.stat().st_mtime_ns for path in stamps}, stamps)

    def test_binary_stamps_ignore_mtime_and_pin_digests_but_track_selected_bytes(self):
        project, _ = fixture(self.root, case=self)
        write_rendered(project)
        data = json.loads((project.tools / "build.json").read_text())
        compiler = data["compilers"][data["default_compiler"]]
        companion = Path(compiler["cc"]).parent / "uopt"
        ignored = companion.with_name("upas")
        for path in (companion, ignored):
            path.write_bytes(b"original")
        manifest = project.tools / "compiler.sha256"
        manifest.write_text(manifest.read_text() + f"aaa  {companion}\nbbb  {ignored}\n")
        stamp = project.tools / "compile/binaries" / (data["default_compiler"] + ".sha256")
        with patch.object(identity, "resolve_tool", return_value=str(project.tools / "as")):
            identity.sync_binaries(project.tools / "build.json")
            for change, expected in (
                ("touch", False),
                ("pin", False),
                ("ignored", False),
                ("companion", True),
                ("binary", True),
            ):
                with self.subTest(change=change):
                    before = stamp.read_bytes(), stamp.stat().st_mtime_ns
                    if change == "touch":
                        Path(compiler["cc"]).touch()
                    elif change == "pin":
                        manifest.write_text(manifest.read_text().replace("aaa", "ccc"))
                    elif change == "ignored":
                        ignored.write_bytes(b"irrelevant")
                    elif change == "companion":
                        companion.write_bytes(b"changed")
                    else:
                        Path(compiler["cc"]).write_bytes(b"changed executable")
                    identity.sync_binaries(project.tools / "build.json")
                    self.assertEqual(stamp.read_bytes() != before[0], expected)
                    self.assertEqual(stamp.stat().st_mtime_ns != before[1], expected)

    def test_kind_projection_table_and_installed_stamp_agreement(self):
        project, _ = fixture(self.root, "sn64", case=self)
        write_rendered(project)
        driver = project.tools / "codegen.py"
        original = driver.read_text()
        scopes = [(kind, sn64) for kind in ("cc", "as") for sn64 in (False, True)]
        baseline = {scope: identity.driver_content(driver, *scope) for scope in scopes}
        for label, old, new, expected in (
            ("comments", "Byte-producing", "Documented byte-producing", set()),
            (
                "assembly logic",
                "content = external_branches(args.source.read_bytes())",
                "content = b'changed assembly'",
                {("as", False)},
            ),
            (
                "C logic",
                "generation = codeflags if sn64 else codegen_flags(flags)",
                "generation = ['-O0'] if not assembly else []",
                set(scopes),
            ),
            (
                "SN64 symbols",
                "symbols, units = read_symbols(args.symbols)",
                "symbols, units = read_symbols(args.source)",
                {("as", True)},
            ),
        ):
            with self.subTest(label=label):
                self.assertIn(old, original)
                driver.write_text(original.replace(old, new))
                self.assertEqual(
                    {scope for scope in scopes if identity.driver_content(driver, *scope) != baseline[scope]}, expected
                )
        driver.write_text(original)
        expected = makefile.driver_settings(project)
        identity.sync_drivers(project.tools / "build.json")
        for kind, sn64 in scopes:
            name = "tools/compile/drivers/" + identity.driver_stamp_name("codegen.py", kind, sn64)
            self.assertEqual((project.root / name).read_text(), expected[name])

    def test_cache_hits_when_stamp_timestamp_or_irrelevant_logic_changes(self):
        project, _ = fixture(self.root, case=self)
        write_rendered(project)
        args = argparse.Namespace(
            recipe=project.tools / "build.json",
            source=project.src / "middle.c",
            output=self.root / "result.o",
            kind="cc",
            version="us",
            unit="src/middle.c",
            non_matching="0",
            depfile=None,
            dep_target=None,
            cache_root=self.root / "cache",
        )
        data = json.loads(args.recipe.read_text())
        driver = project.tools / "codegen.py"
        compiler = Path(data["compilers"][data["default_compiler"]]["cc"])
        produced = []

        def produce(path):
            produced.append(path)
            path.write_bytes(b"object")

        prepared = SimpleNamespace(
            content=b"same preprocessed input",
            source_name="middle.i",
            generation=["-O2"],
            assembler_flags=[],
            assembler_inputs=[],
            inputs=[driver, compiler],
            produce=produce,
        )
        with patch.object(compile, "prepare", return_value=prepared):
            compile.compile_object(args, data)
            self.assertEqual(len(produced), 1)
            for change in ("stamp", "service", "assembly", "binary touch"):
                with self.subTest(change=change):
                    if change == "stamp":
                        stamp = project.tools / "compile/drivers/codegen.cc.native.sha256"
                        stamp.touch()
                    elif change == "service":
                        (project.tools / "compile.py").write_text("# cache reporting edit\n")
                    elif change == "assembly":
                        driver.write_text(
                            driver.read_text().replace(
                                "content = external_branches(args.source.read_bytes())", "content = b'changed'"
                            )
                        )
                    else:
                        compiler.touch()
                    compile.compile_object(args, data)
                    self.assertEqual(len(produced), 1)
            for change in ("source closure", "flags", "compiler bytes", "C driver"):
                if change == "source closure":
                    prepared.content = b"changed preprocessed input"
                elif change == "flags":
                    prepared.generation = ["-O0"]
                elif change == "compiler bytes":
                    compiler.write_bytes(b"new binary")
                else:
                    driver.write_text(
                        driver.read_text().replace(
                            "source_name = Path(args.unit).stem", 'source_name = "changed" + Path(args.unit).stem'
                        )
                    )
                before = len(produced)
                compile.compile_object(args, data)
                self.assertEqual(len(produced), before + 1)

    def test_refresh_migrates_old_graph_preserves_provenance_and_stamps(self):
        project, _ = fixture(self.root, case=self)
        write_rendered(project)
        graph = project.root / "Makefile"
        graph.write_text("old blanket recipe prerequisites\n")
        recipe = project.tools / "build.json"
        provenance = recipe.read_bytes()
        stamps = {path: path.stat().st_mtime_ns for path in (project.tools / "compile").rglob("*") if path.is_file()}
        changed = copy.deepcopy(makefile.description(project))
        changed["compilers"][changed["default_compiler"]]["cflags"] = ["-O0"]
        with patch.object(makefile, "description", return_value=changed):
            setup.refresh_helpers(project)
        self.assertEqual(recipe.read_bytes(), provenance)
        self.assertEqual({path: path.stat().st_mtime_ns for path in stamps}, stamps)
        self.assertIn("compile_identity.py --recipe", graph.read_text())
        self.assertNotIn("$(DRIVERS)", graph.read_text())
        self.assertNotIn(
            "tools/fixture/cc", next(line for line in graph.read_text().splitlines() if line.startswith("$(filter-out"))
        )
        before = graph.stat().st_mtime_ns
        setup.refresh_helpers(project)
        self.assertEqual(graph.stat().st_mtime_ns, before)
