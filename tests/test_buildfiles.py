"""Generated build data: raw slices between published C rows, and the unit table."""

import re
from dataclasses import replace

from tests.project_fixture import ProjectCase
from unbake import buildfiles, config

ROWS = {"alpha": 0x40, "beta": 0x4C, "gamma": 0x58}
ROM_END = 0x64


class BuildfileTests(ProjectCase):
    versions = ("us", "eu")

    def publish(self, name: str, versions: tuple[str, ...]) -> None:
        (self.project.src / f"{name}.c").write_text(f"int {name}(void) {{ return 0; }}\n")
        for version in versions:
            path = self.project.version(version).split
            path.write_text(path.read_text().replace(f"asm, {name}]", f"c, {name}]"))

    def slices(self, version: str) -> set[tuple[int, int]]:
        text = buildfiles.slices_mk(config.load(self.project.root), version)
        return {(int(a), int(b)) for a, b in re.findall(r"^S_\w+ := (\d+) (\d+)$", text, re.M)}

    def test_slices_cover_exactly_the_bytes_no_unit_covers(self) -> None:
        cases = [
            ("all asm", (), {(0, ROM_END)}),
            ("middle unit splits the run", ("beta",), {(0, 0x4C), (0x58, 0xC)}),
            ("adjacent units leave one run", ("alpha", "beta"), {(0, 0x40), (0x58, 0xC)}),
            ("last unit shortens the run", ("gamma",), {(0, 0x58)}),
            ("all C leaves only the header", ("alpha", "beta", "gamma"), {(0, 0x40)}),
        ]
        for label, published, expected in cases:
            with self.subTest(label):
                self.setUp()
                for name in published:
                    self.publish(name, self.versions)
                self.assertEqual(self.slices("us"), expected)

    def test_version_only_row_changes_only_that_version(self) -> None:
        self.publish("beta", ("us",))
        self.assertEqual(self.slices("us"), {(0, 0x4C), (0x58, 0xC)})
        self.assertEqual(self.slices("eu"), {(0, ROM_END)})

    def test_units_table_lists_only_exceptions(self) -> None:
        for name in ("alpha", "beta"):
            self.publish(name, self.versions)
        plain = buildfiles.units_mk(config.load(self.project.root))
        self.assertNotIn("alpha", plain)
        flagged = buildfiles.units_mk(replace(config.load(self.project.root), unit_flags={"alpha": ("-O1",)}))
        self.assertIn("$(B)/src/alpha.o $(B)/units/alpha.bin: UNIT_CODEGEN := -O1", flagged)
        self.assertNotIn("beta", flagged)

    def test_flags_with_shell_characters_are_refused(self) -> None:
        with self.assertRaisesRegex(config.Held, "buildfiles.flag"):
            buildfiles.words(["-DX=$(HOME)"])
