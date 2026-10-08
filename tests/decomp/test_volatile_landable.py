"""Volatile makes a candidate not landable in compare output; hints never propose it. No native work."""

import unittest
from pathlib import Path

from unbake.decomp import checks
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
        self.assertIn("volatile storage is never allowed", result.document()["landable_blockers"][0])
        self.assertTrue(result.lines()[0].startswith("NOT LANDABLE: "))
        self.assertIn("volatile storage is never allowed", result.lines()[0])

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


if __name__ == "__main__":
    unittest.main()
