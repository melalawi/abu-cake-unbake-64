"""Data renames preserve proved cross-VERSION addresses."""

import tempfile
import unittest
from pathlib import Path
from typing import cast

from tests.layout.test_split import ProjectFixture
from unbake.layout import split_edits
from unbake.project.config import Held, Project


class DataProjectFixture(ProjectFixture):
    names_from: str = "us"


class DataRenameTests(unittest.TestCase):
    def test_rename_uses_agreeing_anchors_and_refuses_uncertainty(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = DataProjectFixture(Path(temporary))
            project = cast(Project, fixture)
            fixture.names_from = "us"
            project.version("us").symbols.write_text("left = 0x80001030;\ndata = 0x80001034;\nright = 0x80001038;\n")
            target = project.version("eu").symbols
            cases = (
                ("left = 0x80001130;\nD_80001134 = 0x80001134;\nright = 0x80001138;\n", None),
                ("left = 0x80001130;\nD_80001134 = 0x80001134;\nright = 0x8000113C;\n", "correspondence"),
                ("left = 0x80001130;\nright = 0x80001138;\n", "missing or ambiguous"),
                (
                    "left = 0x80001130;\nD_80001134 = 0x80001134;\nalias = 0x80001134;\nright = 0x80001138;\n",
                    "missing or ambiguous",
                ),
                (
                    "left = 0x80001130;\nother_left = 0x8000112C;\nD_80001134 = 0x80001134;\nright = 0x80001138;\n",
                    "correspondence",
                ),
            )
            for contents, expected in cases:
                with self.subTest(contents=contents):
                    project.version("us").symbols.write_text(
                        "left = 0x80001030;\nother_left = 0x80001030;\ndata = 0x80001034;\nright = 0x80001038;\n"
                    )
                    target.write_text(contents)
                    if expected:
                        with self.assertRaisesRegex(Held, expected):
                            split_edits.rename(project, "data", "shared_data")
                    else:
                        edits = split_edits.rename(project, "data", "shared_data")
                        symbols = [edit for edit in edits if edit.path == target]
                        self.assertEqual(len(symbols), 1)
                        self.assertIn("shared_data = 0x80001134", symbols[0].after)

            fixture.names_from = "eu"
            with self.assertRaisesRegex(Held, "data symbol data: missing in names_from VERSION eu"):
                split_edits.rename(project, "data", "shared_data")
