"""Version port refusals and transactional publication after real proof boundaries."""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import patch

from tests.layout.test_split import ProjectFixture
from unbake.layout import port, split
from unbake.project.config import Policy, Project


class PortTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.fixture = ProjectFixture(self.root)
        self.project = cast(Project, self.fixture)
        self.policy = cast(Policy, self.fixture.policy)
        self.fixture.version("eu").macros = ("VERSION_EU", "VERSION_EUROPE")
        self.fixture.layout("us", [(0x10, "c", "alpha"), (0x20, "data", "pool")])
        self.fixture.layout("eu", [(0x10, "asm", "alpha"), (0x20, "data", "pool")])
        self.project.src.mkdir()
        (self.project.src / "alpha.c").write_text("void alpha(void) {}\n")
        self.scratch = self.root / "scratch"

    def test_inventory_includes_equal_and_different_named_targets(self) -> None:
        rows = port.candidates(self.project, "us", ["eu"])
        self.assertEqual([(row.function, row.identity) for row in rows], [("alpha", "identical")])
        rom = self.project.version("eu").baserom
        data = bytearray(rom.read_bytes())
        data[0x10] ^= 1
        rom.write_bytes(data)
        with patch.object(port, "relocation_masks", return_value={}):
            rows = port.candidates(self.project, "us", ["eu"])
        self.assertEqual((rows[0].identity, rows[0].differing_words), ("different", 1))

    def test_only_relocation_fields_are_masked(self) -> None:
        left, right = (split.functions(self.project, v)[0] for v in ("us", "eu"))
        rom = self.project.version("eu").baserom
        data = bytearray(rom.read_bytes())
        data[0x13] ^= 1
        rom.write_bytes(data)
        with patch.object(port, "relocation_masks", return_value={0: 0xFFFF}):
            self.assertEqual(port.identity(self.project, left, right)[0], "relocations")
        data[0x10] ^= 1
        rom.write_bytes(data)
        with patch.object(port, "relocation_masks", return_value={0: 0xFFFF}):
            self.assertEqual(port.identity(self.project, left, right)[0], "different")

    def test_difference_without_version_branch_refuses_by_name_before_try(self) -> None:
        row = port.candidates(self.project, "us", ["eu"])[0]
        row = port.Candidate(row.function, row.source, row.target, "different", 1)
        with patch("unbake.decomp.trial.try_draft") as trial:
            lines = port.port(self.project, self.policy, [row], self.scratch, apply=True)
        trial.assert_not_called()
        self.assertIn("HELD(port): alpha VERSION eu: target differs", lines[0])
        self.assertIn(", asm, alpha]", self.project.version("eu").split.read_text())

    def test_matching_branch_requires_identical_target_proof(self) -> None:
        row = port.candidates(self.project, "us", ["eu"])[0]
        row = port.Candidate(row.function, row.source, row.target, "different", 1)
        (self.project.src / "alpha.c").write_text("#if defined(VERSION_EU)\nvoid alpha(void) {}\n#endif\n")
        with patch("unbake.decomp.trial.try_draft", return_value=SimpleNamespace(identical_everywhere=False)):
            lines = port.port(self.project, self.policy, [row], self.scratch, apply=True)
        self.assertIn("target-version try is not identical", lines[0])
        self.assertIn(", asm, alpha]", self.project.version("eu").split.read_text())

    def test_bulk_stages_only_proved_rows_and_preserves_other_lines(self) -> None:
        row = port.candidates(self.project, "us", ["eu"])[0]
        before = self.project.version("eu").split.read_text()
        work = self.scratch / "alpha-eu"
        work.mkdir(parents=True)
        (work / "alpha.o").touch()
        with (
            patch.object(port, "required_placements", return_value={}),
            patch("unbake.decomp.trial.try_draft", return_value=SimpleNamespace(identical_everywhere=True)) as trial,
        ):
            lines = port.port(self.project, self.policy, [row], self.scratch, apply=True)
        self.assertEqual(self.project.version("eu").split.read_text(), before.replace(", asm, alpha]", ", c, alpha]"))
        self.assertEqual(trial.call_args.kwargs["versions"], ["eu"])
        self.assertIn("staged 1 proved rows", lines[-1])

    def test_missing_alias_that_conflicts_with_canonical_name_is_refused(self) -> None:
        row = port.candidates(self.project, "us", ["eu"])[0]
        work = self.scratch / "alpha-eu"
        work.mkdir(parents=True)
        (work / "alpha.o").touch()
        with (
            patch.object(port, "required_placements", return_value={"alias": row.target.address}),
            patch("unbake.decomp.trial.try_draft", return_value=SimpleNamespace(identical_everywhere=True)),
        ):
            lines = port.port(self.project, self.policy, [row], self.scratch, apply=True)
        self.assertIn("missing extern alias shares an address with alpha", lines[0])
        self.assertIn(", asm, alpha]", self.project.version("eu").split.read_text())

    def test_missing_extern_bases_are_proved_from_golden_relocations(self) -> None:
        row = port.candidates(self.project, "us", ["eu"])[0]
        rom = self.project.version("eu").baserom
        data = bytearray(rom.read_bytes())
        data[0x10:0x20] = bytes.fromhex("3c028001244223440c00246800000000")
        rom.write_bytes(data)
        data_symbol = {"name": "data", "section": 0, "value": 0, "size": 0, "info": 16}
        helper_symbol = dict(data_symbol, name="helper")
        obj = SimpleNamespace(
            section=lambda name: 1,
            content=lambda section: bytes.fromhex("3c020000244200040c00000000000000"),
            relocations=lambda section: [(0, 5, data_symbol), (4, 6, data_symbol), (8, 4, helper_symbol)],
        )
        with patch.object(port, "Object", return_value=obj):
            result = port.required_placements(self.project, row, self.root / "compiled.o")
        self.assertEqual(result, {"data": 0x80012340, "helper": 0x800091A0})

    def test_branch_must_be_active_target_macro(self) -> None:
        self.assertFalse(port.has_branch(self.project, "#if defined(VERSION_DE)\n", "eu"))
        self.assertFalse(port.has_branch(self.project, "#if !defined(VERSION_EU)\n", "eu"))
        self.assertTrue(port.has_branch(self.project, "#if defined(VERSION_EUROPE)\n", "eu"))

    def test_ambiguous_or_missing_target_never_gets_a_row(self) -> None:
        self.fixture.layout("eu", [(0x10, "asm", "other"), (0x20, "data", "pool")])
        self.assertEqual(port.candidates(self.project, "us", ["eu"]), [])
