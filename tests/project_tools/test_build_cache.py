"""Content-key reuse and cold-graph process amortization regressions."""

from __future__ import annotations

import argparse
import json
import os
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
        self.root = Path(temporary.name).resolve()
        self.addCleanup(os.chdir, Path.cwd())
        os.chdir(self.root)
        compile.tool_digest.cache_clear()

    def test_equal_preprocessed_versions_reuse_objects_and_codegen_invalidates(self) -> None:
        project, _ = fixture(self.root, "sn64", case=self)
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

    def test_cold_graph_batches_sources_and_preserves_incremental_rules(self) -> None:
        project, _ = fixture(self.root, case=self)
        write_rendered(project)
        graph = (self.root / "Makefile").read_text()
        self.assertIn("--batch", graph)
        self.assertIn("C_COLD", graph)
        self.assertIn("--kind cc", graph)
