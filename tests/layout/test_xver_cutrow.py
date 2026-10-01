"""Existing named text rows retain their explicit trailing words."""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from tests.layout.test_xver import Project
from unbake.decomp.needs import PlacementNeed, SymbolNeed
from unbake.layout import xver
from unbake.project.config import Held


class TrialCutRowTests(unittest.TestCase):
    def test_callee_zero_tail_retains_named_row_without_hiding_merged_code(self) -> None:
        body = bytes.fromhex("24020001 03e00008 00000000")
        for kind, padding, merged in (
            ("c", 4, False),
            ("c", 12, False),
            ("asm", 4, False),
            ("asm", 12, False),
            ("asm", 4, True),
            ("c", 4, True),
        ):
            with self.subTest(kind=kind, padding=padding, merged=merged), tempfile.TemporaryDirectory() as directory:
                project = Project(Path(directory))
                for version in project.versions:
                    project.layout(
                        version,
                        [(0x40, "asm", "caller"), (0x50, kind, "callee"), (0x5C + padding, "data", "tail")],
                        symbols="caller = 0x80200000;\ncallee = 0x80200010;\n",
                    )
                    project.image(
                        version,
                        [(0x40, body), (0x50, body + (bytes.fromhex("24030002") if merged else bytes(4)))],
                    )
                trial = SimpleNamespace(
                    function="caller",
                    needs=[SymbolNeed("eu-x", "callee", 0x80200010, 0, ".text", "func", len(body), "jump")],
                )
                before = {item.split: item.split.read_bytes() for item in project.maps.values()}
                if not merged:
                    span = xver.callee_span(project, "callee", "eu-x")
                    assert span is not None
                    self.assertEqual(span.end, 0x5C + padding)
                    self.assertFalse(
                        any(isinstance(need, PlacementNeed) for need in xver.needs(project, "caller", trial))
                    )
                else:
                    # An entry inside a row still requires a cut; named row
                    # ownership must not silently accept an interior entry.
                    item = project.version("eu-x")
                    item.symbols.write_text("caller = 0x80200000;\ncallee = 0x80200014;\n")
                    trial.needs = [SymbolNeed("eu-x", "callee", 0x80200014, 0, ".text", "func", 12, "jump")]
                    if kind == "c":
                        with self.assertRaisesRegex(Held, "cannot cut c row callee"):
                            xver.needs(project, "caller", trial)
                    else:
                        self.assertTrue(any(need.action == "cut" for need in xver.needs(project, "caller", trial)))
                self.assertEqual(before, {path: path.read_bytes() for path in before})
