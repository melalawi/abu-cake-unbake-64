"""Fixture subprocess paths belong to the supplied cwd."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.preprocessor import output


class PreprocessorDirectoryTests(unittest.TestCase):
    def test_relative_include_uses_subprocess_directory_even_when_parent_is_elsewhere(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = root / "project"
            project.mkdir()
            (project / "build/submit-test/tree/src").mkdir(parents=True)
            relative = "build/submit-test/tree/src/item_04.c"
            (project / relative).write_text("int item_04(void) { return 1; }\n")
            with patch("pathlib.Path.cwd", return_value=root):
                result = output(["cpp", "-P", "-"], input=f'#include "{relative}"\n', cwd=project)
            self.assertIn("int item_04", result.stdout)
