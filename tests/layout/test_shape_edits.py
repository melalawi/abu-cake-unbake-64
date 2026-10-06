"""shape_edits: alignment filler and cut tails become split-row edits, named for the aligned entry."""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tests.layout.test_split import ProjectFixture
from unbake.layout import shape_edits

ROWS = [
    (0x10, "asm", "func_80001000"),  # a framed function
    (0x30, "asm", "func_80001020"),  # a body with no return ...
    (0x3C, "asm", "func_8000102C"),  # ... cut here: its tail
    (0x4C, "asm", "func_8000103C"),  # 4 bytes of filler, then an empty function at 0x80001040
    (0x58, "data", "pool"),
]
CODE = (
    "27bdffe8 afbf0014 0c000000 00000000 8fbf0014 27bd0018 03e00008 00000000"
    " c4a20004 c4c00008 46001082"
    " 46010002 46001081 03e00008 e4820008"
    " a6a5def8 03e00008 00000000"
)
SYMBOLS = "".join(f"{name} = 0x{0x80001000 + start - 0x10:08X}; // type:func\n" for start, _, name in ROWS[:4])


class ShapeEditTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.fixture = ProjectFixture(Path(directory.name))
        self.fixture.src.mkdir()
        (self.fixture.root / "config.toml").write_text("[units]\n")
        self.fixture.names_from = "us"
        self.manifest = self.fixture.root / "unbake-exclusions.json"
        self.manifest.write_text('{"schema": 1, "functions": ["func_8000102C", "func_8000103C", "func_80001000"]}')
        compiler = SimpleNamespace(id="ido-7.1", cflags=("-O2", "-mips2"))
        self.fixture.unit_flags = {}
        self.fixture.compilers = {"ido-7.1": compiler}
        self.fixture.compiler_for = lambda unit: compiler
        image = bytes(0x10) + bytes.fromhex(CODE.replace(" ", "")) + bytes(0x60 - 0x58)
        for version in self.fixture.versions:
            self.fixture.version(version).baserom.write_bytes(image)
            self.fixture.layout(version, ROWS)
            self.fixture.version(version).symbols.write_text(SYMBOLS)

    def run_edits(self) -> list[str]:
        with (
            patch("unbake.pool.run", lambda host, fn, items: [fn(item) for item in items]),
            patch("unbake.land._commit") as commit,
            patch("unbake.config.load", return_value=self.fixture),
            patch("unbake.buildfiles.write", return_value=[]),
        ):
            lines = shape_edits.run(self.fixture, SimpleNamespace())
        self.commits = commit.call_count
        return lines

    def test_refusals_leave_the_split_alone(self) -> None:
        cases = {
            "published C uses the names": (
                lambda: (self.fixture.src / "f.c").write_text("void f(void) { func_8000102C(); func_8000103C(); }\n"),
                "published C uses it",
            ),
            "a symbol already names the entry": (
                lambda: [
                    self.fixture.version(v).symbols.write_text(SYMBOLS + "D_80001040 = 0x80001040;\n")
                    for v in self.fixture.versions
                ],
                "a symbol already names its entry",
            ),
        }
        for name, (arrange, reason) in cases.items():
            with self.subTest(name):
                self.setUp()
                arrange()
                before = self.fixture.version("us").split.read_text()
                lines = self.run_edits()
                self.assertTrue(any(reason in line for line in lines), lines)
                if name == "published C uses the names":
                    self.assertEqual(self.fixture.version("us").split.read_text(), before)
                    self.assertEqual(self.commits, 0)

    def test_renames_need_one_filler_size_in_every_holding_version(self) -> None:
        def scan(version: str, skip: int | None, names: set[str]) -> shape_edits.Scan:
            found = () if skip is None else (shape_edits.Finding(version, "filler", "func_80001004_x", 0, 0, skip),)
            return shape_edits.Scan(version, found, frozenset(names))

        held = {"func_80001004_x"}
        cases = {
            "same filler everywhere": (
                [scan("a", 4, held), scan("b", 4, held)],
                set(),
                {"func_80001004_x": "func_80001008_x"},
            ),
            "sizes differ": ([scan("a", 4, held), scan("b", 8, held)], set(), {}),
            "one version has none": ([scan("a", 4, held), scan("b", None, held)], set(), {}),
            "new name taken": ([scan("a", 4, held | {"func_80001008_x"})], set(), {}),
            "C uses the new name": ([scan("a", 4, held)], {"func_80001008_x"}, {}),
        }
        for name, (scans, used, expected) in cases.items():
            with self.subTest(name):
                self.assertEqual(shape_edits.renames(scans, used), expected)
