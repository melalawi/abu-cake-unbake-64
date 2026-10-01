"""Data renames preserve proved cross-VERSION addresses."""

import tempfile
import unittest
from pathlib import Path

from tests.layout.test_split import ProjectFixture
from unbake.layout import split_edits
from unbake.project.config import Held


class DataRenameTests(unittest.TestCase):
    def test_rename_uses_agreeing_anchors_and_refuses_uncertainty(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = ProjectFixture(Path(temporary))
            project.names_from = "us"
            project.version("us").symbols.write_text("left = 0x80001030;\ndata = 0x80001034;\nright = 0x80001038;\n")
            target = project.version("eu").symbols
            for tail, expected in (("0x80001138", None), ("0x8000113C", "correspondence")):
                with self.subTest(tail=tail):
                    target.write_text(f"left = 0x80001130;\nD_80001134 = 0x80001134;\nright = {tail};\n")
                    if expected:
                        with self.assertRaisesRegex(Held, expected):
                            split_edits.rename(project, "data", "shared_data")
                    else:
                        edits = split_edits.rename(project, "data", "shared_data")
                        symbols = [edit for edit in edits if edit.path == target]
                        self.assertEqual(len(symbols), 1)
                        self.assertIn("shared_data = 0x80001134", symbols[0].after)
