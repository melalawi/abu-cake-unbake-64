"""CLI publication shares Make and submission compiler cache entries."""

from __future__ import annotations

import io
import shutil
import subprocess
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from tests.match.support import MatchFixture
from tests.project.makefile_fixture import fixture
from unbake.cli.main import main
from unbake.match import data_symbols
from unbake.project import build, makefile

COMPILE_OBJECT = build.compile_object
PREPARE = data_symbols.prepare


class PublicationCacheTests(MatchFixture):
    def setUp(self) -> None:
        super().setUp()
        self.compiler_root = Path(self.temporary.name) / "compiler"
        compiler_project, _ = fixture(self.compiler_root)
        shutil.copytree(compiler_project.tools, self.project.tools, dirs_exist_ok=True)
        compiler = compiler_project.compilers["fixture"]
        compiler = replace(
            compiler,
            cc=self.project.tools / "fixture/cc",
            as_=self.project.tools / "fixture/as",
            sha256=self.project.tools / "compiler.sha256",
        )
        self.project = replace(self.project, compilers={"fixture": compiler}, default_compiler="fixture")
        shutil.copyfile(self.compiler_root / "config.toml", self.root / "config.toml")
        (self.project.include[0] / "value.h").write_text("#define VALUE 1\n")
        for name, content in makefile.helpers(self.project).items():
            (self.root / name).write_text(content)

    def cli(self, *arguments: str) -> str:
        output = io.StringIO()
        with (
            patch("unbake.cli.main.config.load", return_value=self.project),
            patch("unbake.cli.main.config.load_policy", return_value=self.policy),
            patch.object(data_symbols, "prepare", PREPARE),
            patch.object(data_symbols, "edits", return_value=[]),
            patch.object(build, "compile_object", wraps=COMPILE_OBJECT) as compiler,
            redirect_stdout(output),
            redirect_stderr(output),
        ):
            self.assertEqual(main(["--project", str(self.root), *arguments]), 0, output.getvalue())
        self.object_requests = compiler.call_count
        return output.getvalue()

    def test_make_object_is_a_hit_for_submit_and_two_cli_match_runs(self) -> None:
        source = self.draft("alpha", "extern int table;\nint alpha(void) { return table; }\n")
        command = [
            "python3",
            str(self.project.tools / "compile.py"),
            "--kind",
            "cc",
            "--recipe",
            str(self.project.tools / "build.json"),
            "--version",
            "us",
            "--unit",
            "src/alpha.c",
            "--source",
            str(source),
            "--output",
            str(self.root / "build/make-alpha.o"),
            "--non-matching",
            "0",
            "--cache-root",
            str(self.policy.cache_root),
        ]
        result = subprocess.run(command, cwd=self.root, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        trace = self.compiler_root / "calls"
        self.assertEqual(trace.read_text().splitlines(), ["cc"])
        for _ in range(2):
            self.cli("match", "submit", str(source))
            self.assertEqual(self.object_requests, len(self.versions))
            before = trace.read_bytes()
            output = self.cli("match", "run")
            self.assertEqual(self.object_requests, len(self.versions))
            self.assertIn("alpha matched on VERSION us, eu", output)
            self.assertEqual(trace.read_bytes(), before)
        self.assertEqual(trace.read_text().splitlines(), ["cc"])

    def test_placed_reference_skips_submission_and_staging_compilation(self) -> None:
        for version in self.versions:
            symbols = self.project.version(version).symbols
            symbols.write_text(symbols.read_text() + "table = 0x80002000;\n")
        source = self.draft("alpha", "extern int table;\nint alpha(void) { return table; }\n")
        self.cli("match", "submit", str(source))
        self.assertEqual(self.object_requests, 0)
        self.cli("match", "run")
        self.assertEqual(self.object_requests, 0)
        self.assertFalse((self.compiler_root / "calls").exists())
