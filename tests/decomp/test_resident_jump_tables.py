"""Draft and trial share resident table mapping and pointer bias handling."""

import struct
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tests.decomp.support import fixture
from unbake.decomp.draft_input import jump_tables
from unbake.project import makefile
from unbake.project.config import Project


def resident(project: Project, bias: int) -> SimpleNamespace:
    version = project.version("us")
    version.split.write_text(
        version.split.read_text().replace("  - [0x80]", "      - [0x100, bin, resident]\n  - [0x10C]")
    )
    image = version.baserom.read_bytes()
    version.baserom.write_bytes(
        image + bytes(0x100 - len(image)) + struct.pack(">III", 0x80001018 - bias, 0x80001018 - bias, 0)
    )
    return SimpleNamespace(
        resident_mappings={"us": [dict(address=0x80003000, start=0x100, end=0x10C, table_entry_bias=bias)]}
    )


class ResidentJumpTableTests(unittest.TestCase):
    def test_draft_reads_biased_resident_entries_and_stops_at_mapping_end(self) -> None:
        for bias in (0, 0x80000000):
            with self.subTest(bias=bias), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary).resolve()
                project, _, _ = fixture(root, words=[0] * 10, case=self)
                recipe = resident(project, bias)
                assembly = "glabel alpha\nlui $at, %hi(jtbl_80003000)\n/* 000058 80001018 00000000 */ nop\n"
                with patch.object(makefile, "recipe", return_value=recipe):
                    output = jump_tables(project, "us", "alpha", assembly)
                self.assertEqual(output.count(".word .L80001018"), 2)
                self.assertEqual(output.count(".L80001018:"), 1)
                # A table can end exactly at the resident mapping boundary.
                recipe.resident_mappings["us"][0]["end"] = 0x108
                with patch.object(makefile, "recipe", return_value=recipe):
                    self.assertEqual(jump_tables(project, "us", "alpha", assembly), output)
