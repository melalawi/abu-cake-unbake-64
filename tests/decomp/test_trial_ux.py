"""Regression for source context and concise VERSION-scoped trial diagnostics."""

import io
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import patch

from unbake.decomp import guide
from unbake.decomp.trial_source import control_context, debug_locations
from unbake.project.config import Project


class TrialUxTests(unittest.TestCase):
    def test_diagnostics_keep_versions_counts_and_source_boundaries(self) -> None:
        project = cast(Project, SimpleNamespace(versions={"us": None, "eu": None}))
        stream = io.StringIO()
        with patch.object(guide, "for_version", return_value="frame: 0x20 bytes"), redirect_stdout(stream):
            output = guide.run(project, "example", None)
        self.assertEqual(stream.getvalue(), "")
        self.assertEqual(output.count("frame:"), 2)
        self.assertIn("VERSION us", output)
        self.assertIn("VERSION eu", output)
        source = "void example(int x) {\n while (x) {\n  if (x > 1) {\n   x--;\n  }\n }\n}\n"
        locations = debug_locations("example.c:4\n  10: 2442ffff addiu v0,v0,-1\n")
        self.assertEqual(locations[16], ("example.c", 4))
        context = control_context(source, locations[16][1])
        self.assertIn("while loop start line 2; loop end line 6", context)
        self.assertIn("if branch start line 3; branch end line 5", context)
        self.assertEqual(control_context(source, 7), "")
        self.assertEqual(Path(locations[16][0]).name, "example.c")
