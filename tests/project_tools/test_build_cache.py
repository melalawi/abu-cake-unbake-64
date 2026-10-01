"""Content-key reuse and cold-graph process amortization regressions."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.project.makefile_fixture import fixture, write_rendered
from unbake.project_tools import compile


class BuildCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.addCleanup(patch.stopall)
        self.root = Path(temporary.name)
        self.addCleanup(os.chdir, Path.cwd())
        os.chdir(self.root)
        compile.tool_digest.cache_clear()

    def test_equal_preprocessed_versions_reuse_objects_and_codegen_invalidates(self) -> None:
        project, _ = fixture(self.root, "sn64")
        write_rendered(project)
        recipe = self.root / "tools/build.json"
        data = json.loads(recipe.read_text())
        data["macros"]["other"] = ["UNUSED_VERSION=2"]
        recipe.write_text(json.dumps(data))
        for version in ("us", "other"):
            include = self.root / "asm" / version / "include"
            include.mkdir(parents=True)
            (include / "macro.inc").write_text("same assembler macros")
        args = argparse.Namespace(
            recipe=recipe,
            output=self.root / "one.o",
            source=self.root / "src/middle.c",
            version="us",
            kind="cc",
            unit="src/middle.c",
            non_matching="0",
            depfile=None,
            cache_root=self.root / "cache",
            symbols=None,
            dep_target=None,
        )
        compile.compile_object(args)
        args.version = "other"
        args.output = self.root / "two.o"
        compile.compile_object(args)
        self.assertEqual((self.root / "one.o").read_bytes(), args.output.read_bytes())
        self.assertEqual((self.root / "calls").read_text().splitlines().count("cc1"), 1)
        compiler = data["compilers"][data["default_compiler"]]
        compiler["cflags"].append("-O1")
        recipe.write_text(json.dumps(data))
        compile.compile_object(args)
        self.assertEqual((self.root / "calls").read_text().splitlines().count("cc1"), 2)
        (self.root / "asm/other/include/macro.inc").write_text("changed assembler macros")
        compile.compile_object(args)
        self.assertEqual((self.root / "calls").read_text().splitlines().count("cc1"), 3)

    def test_cache_service_update_keeps_warm_codegen_receipts(self) -> None:
        project, _ = fixture(self.root)
        write_rendered(project)
        result = subprocess.run(["make", "-j4"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        receipt = self.root / "build/us/obj/src/middle.built"
        before = receipt.stat().st_mtime_ns
        calls = (self.root / "calls").read_text()
        helper = self.root / "tools/cache.py"
        helper.write_text(helper.read_text() + "\n# Cache reader service update.\n")
        result = subprocess.run(["make", "-j4"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(receipt.stat().st_mtime_ns, before)
        self.assertEqual((self.root / "calls").read_text(), calls)

    def test_cold_graph_batches_sources_and_preserves_incremental_rules(self) -> None:
        project, _ = fixture(self.root)
        write_rendered(project)
        result = subprocess.run(["make", "-j4"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("--batch src/middle.c", result.stdout)
        result = subprocess.run(["make", "-j4"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("--batch", result.stdout)
        (self.root / "include/value.h").write_text("#define VALUE 2\n")
        result = subprocess.run(["make", "-j4"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("--kind cc", result.stdout)
        self.assertNotIn("--batch", result.stdout)
