"""Regressions for standalone drafts and complete assembly inputs."""

import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from tests.decomp.test_trial import fixture
from unbake.decomp import guide, m2c
from unbake.decomp.draft_input import whole_body
from unbake.project.config import Held


class DraftInputTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(dir=os.environ["TMPDIR"])
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.project, self.policy, _ = fixture(self.root)
        self.policy.state_root = self.root / "state"
        self.policy.m2c = self.root / "m2c"
        self.policy.m2c.write_text('#!/bin/sh\nprintf "typedef int s32;\\nint alpha(void) { return 1; }\\n"\n')
        self.policy.m2c.chmod(0o755)

    def test_duplicate_typedef_dependencies_are_expanded_once(self) -> None:
        for name in ("one", "two"):
            (self.project.include[0] / f"{name}.h").write_text(f'#include "types.h"\nstruct {name} {{ s32 v; }};\n')
        self.policy.m2c.write_text(
            '#!/bin/sh\nprintf "typedef int s32;\\n'
            'int alpha(struct one *a, struct two *b) { return a->v + b->v; }\\n"\n'
        )
        source = m2c.draft(self.project, self.policy, "alpha", "us", self.root / "draft")
        self.assertEqual(source.read_text().count("typedef int s32;"), 1)
        self.assertIn("struct one", source.read_text())
        self.assertIn("struct two", source.read_text())

    def test_guide_infers_only_a_single_version(self) -> None:
        with patch.object(guide, "load_policy", return_value=self.policy), redirect_stdout(io.StringIO()):
            self.assertEqual(guide.run(self.project, "alpha", None), guide.run(self.project, "alpha", "us"))
        project = replace(self.project, versions=("us", "eu"))
        with self.assertRaisesRegex(Held, "--version is ambiguous; choose from us, eu"):
            guide.run(project, "alpha", None)

    def test_draft_announces_function_filename_and_written_path(self) -> None:
        with redirect_stdout(io.StringIO()) as output:
            source = m2c.draft(self.project, self.policy, "alpha", "us", self.root / "draft")
        self.assertTrue(source.is_file())
        self.assertEqual(source.name, "alpha.c")
        self.assertIn(f"draft_path: {source}", output.getvalue())
        self.assertIn("source filename: alpha.c", output.getvalue())

    def test_internal_function_markers_preserve_the_complete_body(self) -> None:
        assembly = "glabel alpha\nbnez $v0, tail\nendlabel alpha\nglabel tail\njr $ra\nnop\nendlabel tail\n"
        body = whole_body(assembly, "alpha")
        self.assertEqual(body.count("glabel"), 1)
        self.assertIn("bnez $v0, .L_tail", body)
        self.assertIn(".L_tail:\njr $ra\nnop", body)
        self.assertNotIn("endlabel", body)
