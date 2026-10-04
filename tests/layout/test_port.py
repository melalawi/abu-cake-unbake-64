"""Version port refusals and transactional publication after real proof boundaries."""

import tempfile
import unittest
from pathlib import Path
from typing import cast
from unittest.mock import patch

from tests.layout.test_split import ProjectFixture
from unbake.config import Host, Project
from unbake.layout import port, split


class PortTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.fixture = ProjectFixture(self.root)
        self.project = cast(Project, self.fixture)
        self.policy = cast(Host, self.fixture.policy)
        self.fixture.version("eu").macros = ("VERSION_EU", "VERSION_EUROPE")
        self.fixture.layout("us", [(0x10, "c", "alpha"), (0x20, "data", "pool")])
        self.fixture.layout("eu", [(0x10, "asm", "alpha"), (0x20, "data", "pool")])
        self.project.src.mkdir()
        (self.project.src / "alpha.c").write_text("void alpha(void) {}\n")
        self.scratch = self.root / "scratch"

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
