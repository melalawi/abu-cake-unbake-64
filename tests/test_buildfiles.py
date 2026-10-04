"""Generated build data: slice runs of unmatched ROM bytes and the unit table."""

import re
import shutil
from pathlib import Path

from tests.kit import TempCase
from unbake import buildfiles, config

FIXTURE = Path(__file__).parent / "fixture"
ROM_END = 0x64
CODE = {"alpha": (0x40, 0x4C), "beta": (0x4C, 0x58), "gamma": (0x58, 0x64)}


def layout_text(matched: dict[str, list[str] | None]) -> str:
    """matched: member -> versions where it is C, None for all versions; absent members stay asm."""
    lines = ["schema = 1", "cap = 3", "[[group]]", 'name = "main"', 'segment = "main"', 'evidence = "default"']
    members = []
    for name in CODE:
        if name not in matched:
            members.append(f'"{name}"')
        elif matched[name] is None:
            members.append(f'{{ name = "{name}", c = true }}')
        else:
            members.append(f'{{ name = "{name}", c = {matched[name]!r} }}'.replace("'", '"'))
    lines.append("members = [" + ", ".join(members) + "]")
    return "\n".join(lines) + "\n"


def runs(files: dict[Path, bytes], version: str) -> set[tuple[int, int]]:
    text = next(data for path, data in files.items() if path.name == "slices.mk" and path.parent.name == version)
    text = text.decode()
    starts = re.findall(r"^(\w+)_START := (0x[0-9A-Fa-f]+)$", text, re.M)
    sizes = dict(re.findall(r"^(\w+)_SIZE := (0x[0-9A-Fa-f]+)$", text, re.M))
    return {(int(start, 16), int(sizes[name], 16)) for name, start in starts}


# DRAFT interface: the layout.toml spelling of matched members and the config.toml [units] rows.
class SliceRunTests(TempCase):
    def project(self, matched: dict[str, list[str] | None], units: str = "") -> config.Project:
        shutil.copytree(FIXTURE, self.root, dirs_exist_ok=True)
        (self.root / "layout.toml").write_text(layout_text(matched))
        text = (self.root / "config.toml").read_text().replace("[units]\n", "[units]\n" + units)
        (self.root / "config.toml").write_text(text)
        return config.load(self.root)

    def test_runs_of_unmatched_bytes(self) -> None:
        header = (0, 0x40)
        cases = [
            ("all asm", {}, {header, (0x40, 0x24)}),
            ("middle unit splits the run", {"beta": None}, {header, (0x40, 0xC), (0x58, 0xC)}),
            ("adjacent C units leave one run", {"alpha": None, "beta": None}, {header, (0x58, 0xC)}),
            ("first unit moves the run start", {"alpha": None}, {header, (0x4C, 0x18)}),
            ("last unit shortens the run", {"gamma": None}, {header, (0x40, 0x18)}),
            ("all C leaves only the header", {"alpha": None, "beta": None, "gamma": None}, {header}),
        ]
        for label, matched, expected in cases:
            with self.subTest(label):
                self.assertEqual(runs(buildfiles.generate(self.project(matched)), "us"), expected)
                shutil.rmtree(self.root)

    def test_runs_never_cross_the_segment_edge(self) -> None:
        # The header segment ends at 0x40 where code starts; both stay separate runs.
        starts = sorted(start for start, _ in runs(buildfiles.generate(self.project({})), "us"))
        self.assertEqual(starts, [0, 0x40])

    def test_version_only_member_changes_only_that_version(self) -> None:
        files = buildfiles.generate(self.project({"beta": ["us"]}))
        self.assertEqual(runs(files, "us"), {(0, 0x40), (0x40, 0xC), (0x58, 0xC)})
        self.assertEqual(runs(files, "us-rev1"), {(0, 0x40), (0x40, 0x24)})

    def test_units_table_lists_only_non_default_units(self) -> None:
        project = self.project({"alpha": None, "beta": None}, units='alpha = { compiler = "ido-7.1", flags = ["-O1"] }\n')
        files = buildfiles.generate(project)
        text = next(data for path, data in files.items() if path.name == "units.mk").decode()
        self.assertIn("alpha", text)
        self.assertNotIn("beta", text)
        self.assertIn("-O1", text)

    def test_no_units_table_rows_without_exceptions(self) -> None:
        files = buildfiles.generate(self.project({"alpha": None}))
        text = next(data for path, data in files.items() if path.name == "units.mk").decode()
        self.assertNotIn("alpha", text)
