"""Volatile makes a candidate not landable in compare output; hints never propose it. No native work."""

import dataclasses
import unittest
from pathlib import Path

from unbake.config import Project
from unbake.decomp import checks
from unbake.decomp.needs import GuardFinding
from unbake.work import hints
from unbake.work.compare import Compared
from unbake.work.score import measure_words

HEADER = "typedef volatile unsigned int vu32;\n#define REG volatile unsigned int\n"


def compared(source: str, preprocessed: str | None = None) -> Compared:
    broken = [f for f in checks.run(source, preprocessed, "f") if f.fakematch is None]
    return Compared(
        "f",
        Path("f.c"),
        "0",
        {"us": measure_words("us", b"\0\0\0\0", b"\0\0\0\0")},
        [checks.message(f) for f in broken],
        0.0,
        "gcc",
        [checks.plain(f) for f in broken],
    )


class VolatileLandableTest(unittest.TestCase):
    def assert_blocked(self, source: str, preprocessed: str | None = None) -> None:
        result = compared(source, preprocessed)
        self.assertFalse(result.landable)
        self.assertFalse(result.document()["landable"])
        self.assertIn(
            "volatile is only allowed for SDK hardware access or in SDK library units",
            result.document()["landable_blockers"][0],
        )
        self.assertTrue(result.lines()[0].startswith("NOT LANDABLE: "))
        self.assertIn("volatile is only allowed for SDK hardware access or in SDK library units", result.lines()[0])

    def test_local_variable(self) -> None:
        self.assert_blocked("void f(void) { volatile int x; x = 1; }")

    def test_cast(self) -> None:
        self.assert_blocked("int f(int *p) { return (volatile int)*p; }")

    def test_local_typedef_and_macro(self) -> None:
        self.assert_blocked("typedef volatile int vint;\nvoid f(void) { vint x; x = 1; }")
        self.assert_blocked("#define V volatile\nvoid f(void) { V int x; x = 1; }")

    def test_header_typedef(self) -> None:
        source = "void f(void) {\n    vu32 x;\n    x = 1;\n}\n"
        self.assert_blocked(source, HEADER + source)
        self.assertEqual(checks.run(source, HEADER + source, "f")[0].line, 2)

    def test_header_macro_expanding_inside_function(self) -> None:
        source = "void f(void) {\n    REG x;\n    x = 1;\n}\n"
        expanded = HEADER + "void f(void) {\n    volatile unsigned int x;\n    x = 1;\n}\n"
        self.assert_blocked(source, expanded)

    def test_clean_source_is_landable_and_unchanged(self) -> None:
        source = "void f(void) { int x; x = 1; }"
        result = compared(source, HEADER + source)
        self.assertTrue(result.landable)
        self.assertTrue(result.document()["landable"])
        self.assertEqual(result.document()["landable_blockers"], [])
        self.assertFalse(any(line.startswith("NOT LANDABLE") for line in result.lines()))
        self.assertEqual(checks.run(source), checks.run(source, HEADER + source, "f"))

    def test_hint_filter_drops_volatile_fixes(self) -> None:
        bad = {"id": "x", "version": "us", "fix": "volatile hw pointer plus volatile local"}
        good = {"id": "y", "version": "us", "fix": "split one variable into two"}
        self.assertEqual(
            hints.technique_lines((bad, good)), ["this helped before: split one variable into two (y; us)"]
        )
        self.assertFalse(hints.landable_fix("make it Volatile"))
        self.assertTrue(hints.landable_fix("drop -fno-strength-reduce"))


RCP = (
    "typedef volatile unsigned int vu32;\n"
    "#define PHYS_TO_K1(x) ((unsigned)(x) | 0xA0000000)\n"
    "#define IO_READ(addr) (*(vu32 *)PHYS_TO_K1(addr))\n"
    "#define IO_WRITE(addr, data) (*(vu32 *)PHYS_TO_K1(addr) = (data))\n"
    "#define PI_STATUS_REG 0x04600010\n"
)


class VolatileRuleTest(unittest.TestCase):
    def volatile(self, source: str, preprocessed: str | None = None, *, library: bool = False) -> list[GuardFinding]:
        return [f for f in checks.run(source, preprocessed, "f", library=library) if f.rule == "volatile-storage"]

    def test_io_macros_in_game_unit_are_allowed(self) -> None:
        source = "int f(void) {\n    IO_WRITE(PI_STATUS_REG, 2);\n    return IO_READ(PI_STATUS_REG);\n}\n"
        self.assertEqual(self.volatile(source, RCP + source), [])

    def test_hardware_cast_to_volatile_sdk_type_is_allowed(self) -> None:
        source = "int f(void) {\n    return *(vu32 *)PHYS_TO_K1(PI_STATUS_REG);\n}\n"
        self.assertEqual(self.volatile(source, RCP + source), [])
        expanded = "int f(void) {\n    return *(vu32 *)((unsigned)(0x04600010) | 0xA0000000);\n}\n"
        self.assertEqual(self.volatile(source, RCP + expanded), [])
        self.assertEqual(self.volatile("int x = *(volatile unsigned *)PI_STATUS_REG;"), [])

    def test_volatile_local_in_library_unit_is_allowed_and_in_game_unit_refused(self) -> None:
        source = "void __osLeoInterrupt(void) {\n    volatile u32 pi_stat;\n    pi_stat = 1;\n}\n"
        self.assertEqual(self.volatile(source, library=True), [])
        refused = self.volatile(source)
        self.assertEqual([f.line for f in refused], [2])
        self.assertIn("volatile is only allowed for SDK hardware access or in SDK library units", refused[0].text)
        self.assertIn("game unit, not declared in [build].library_units", refused[0].text)

    def test_typedef_global_in_game_unit_is_refused(self) -> None:
        source = "vu32 counter;\nvoid f(void) { counter = 1; }\n"
        found = self.volatile(source, RCP + source)
        self.assertEqual([f.line for f in found], [1])
        self.assertIn("volatile is only allowed", found[0].text)
        self.assertEqual(self.volatile(source, RCP + source, library=True), [])

    def test_volatile_through_macro_in_game_unit_is_refused(self) -> None:
        source = "#define V volatile\nvoid f(void) { V int x; x = 1; }\n"
        self.assertIn(2, [f.line for f in self.volatile(source)])

    def test_sizeof_and_return_qualifier_are_refused(self) -> None:
        self.assertEqual(len(self.volatile("char pad[4 - sizeof(volatile int)];")), 1)
        self.assertEqual(len(self.volatile("volatile int f(void) { return 0; }")), 1)

    def test_library_units_are_declared_by_glob(self) -> None:
        root = Path("/project")
        project = Project(
            root, "p", "P", "us", ("us",), {}, "c", {}, {}, "id", 1, (), (), (), library_units=("libultra/*", "leo/io")
        )
        for source, expected in (("libultra/leointerrupt.c", True), ("leo/io.c", True), ("game/main.c", False)):
            self.assertEqual(project.library(project.src / source), expected, source)
        self.assertFalse(dataclasses.replace(project, library_units=()).library(project.src / "libultra/a.c"))


if __name__ == "__main__":
    unittest.main()
