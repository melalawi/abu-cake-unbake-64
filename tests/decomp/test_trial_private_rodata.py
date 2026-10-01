"""Compiler constants remain linkable when target ownership is unproved."""

import io
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import patch

from tests.decomp.test_trial import READELF, assemble, fixture
from unbake.decomp import needs, trial, trial_compare, trial_link
from unbake.project import build
from unbake.project.config import Policy, Project


class PrivateRodataTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.scratch = self.directory / "scratch"

    def test_private_compiler_constants_score_without_target_owners(self) -> None:
        from dataclasses import replace

        for ident, section in (("ido-7.1", ".rodata"), ("gcc-2.8.1-sn64", ".rdata")):
            with self.subTest(compiler=ident):
                directory = self.directory / (ident + "-private")
                directory.mkdir()
                project, policy, source = fixture(directory, versions=("us", "us-rev1", "eu", "eu-x", "de"))
                compiler_config = replace(project.compiler_for(source), id=ident)
                project = replace(project, compilers={ident: compiler_config}, default_compiler=ident)

                def compiler(
                    p: Project, policy: SimpleNamespace, source: Path, version: str, out: Path, section: str = section
                ) -> Path:
                    body = (
                        ".set noreorder\n.text\n.globl alpha\n.type alpha, @function\nalpha:\n"
                        "lui $v0, %hi(table)\naddiu $v0, $v0, %lo(table)\n"
                        "lui $v1, %hi(literal)\nlwc1 $f0, %lo(literal)($v1)\n"
                        "case0: jr $ra\nnop\n.size alpha, .-alpha\n"
                        + f".section {section}\ntable:\n"
                        + ".word case0\n" * 6
                        + "literal: .float 1.2345\n"
                    )
                    shutil.copyfile(assemble(out.parent, "compiled", body), out)
                    return out

                scratch = self.scratch / ident
                with patch.object(build, "compile_object", side_effect=compiler), redirect_stdout(io.StringIO()):
                    result = trial.try_draft(project, cast(Policy, policy), source, scratch)
                self.assertEqual(set(result.compares), set(project.versions))
                self.assertFalse(result.identical_everywhere)
                self.assertFalse(any(isinstance(need, needs.RodataNeed) for need in result.needs))
                self.assertTrue(any("publication placement proof" in line for line in result.preconditions))
                for version in project.versions:
                    self.assertGreater(result.compares[version].of, 0)
                    linked = next(scratch.glob(f"alpha.*/{version}/trial.elf"))
                    output = trial_link.inspect(linked, READELF, linked.parent)
                    placed = next(row for row in output.sections.values() if row.name == section)
                    table = trial_compare.words(linked.read_bytes()[placed.offset : placed.offset + 24])
                    address = trial_link.function_symbol(output, "alpha").address
                    self.assertEqual(table, [address + 16] * 6)
