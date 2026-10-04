"""Compiler driver argv per kind and per-unit override (the one source for Makefile rules and the runner)."""

import unittest
from pathlib import Path
from types import SimpleNamespace

from unbake.compilers import drivers

ROOT = Path("/p")


def compiler(kind: str, cflags: tuple[str, ...]) -> SimpleNamespace:
    return SimpleNamespace(
        id=kind, kind=kind, cc=ROOT / f"tools/{kind}/cc", as_=ROOT / f"tools/{kind}/as", cflags=cflags
    )


def project(**units: SimpleNamespace) -> SimpleNamespace:
    default = compiler("gcc", ("-O2", "-G0"))
    return SimpleNamespace(
        root=ROOT,
        include=(ROOT / "include",),
        cppflags=("-DFOO",),
        asflags=("-EB", "-mips2"),
        sn64_asflags=("-EB", "-march=vr4300"),
        default_compiler="gcc",
        compilers={"gcc": default},
        compiler_for=lambda unit: units.get(Path(unit).stem, default),
        version=lambda v: SimpleNamespace(macros=(f"VERSION_{v.upper()}",)),
    )


# DRAFT interface: argv(project, unit, version) -> list of command steps (cpp, compile, [assemble]).
class DriverArgvTests(unittest.TestCase):
    def steps(self, p: SimpleNamespace, unit: str = "alpha") -> list[list[str]]:
        return drivers.argv(p, unit, "us")

    def test_argv_shape_per_kind(self) -> None:
        cases = [
            ("gcc", 3, "-quiet", "as"),
            ("ido", 2, None, None),
            ("sn64", 3, "-quiet", "asn64"),
        ]
        for kind, count, quiet, assembler in cases:
            with self.subTest(kind):
                p = project(alpha=compiler(kind, ("-O1", "-G0")))
                steps = self.steps(p)
                self.assertEqual(len(steps), count)
                preprocess, compile_step = steps[0], steps[1]
                self.assertIn("-MMD", preprocess)
                self.assertIn("-DVERSION_US", preprocess)
                self.assertIn("-DFOO", preprocess)
                self.assertIn(f"-I{ROOT / 'include'}", preprocess)
                self.assertIn(str(ROOT / f"tools/{kind}/cc"), compile_step)
                self.assertEqual([f for f in compile_step if f in ("-O1", "-G0")], ["-O1", "-G0"])
                if quiet:
                    self.assertIn(quiet, compile_step)
                if assembler:
                    self.assertTrue(any(assembler in part for part in steps[2]))
                    self.assertTrue(set(project().asflags) <= set(steps[2]) or kind == "sn64")
                else:
                    self.assertFalse(any("-quiet" in step for step in steps))

    def test_sn64_assembles_with_its_own_flags_through_n64link(self) -> None:
        steps = self.steps(project(alpha=compiler("sn64", ("-O2",))))
        self.assertIn("asn64", steps[2])
        self.assertIn("-march=vr4300", steps[2])
        self.assertNotIn("-mips2", steps[2])

    def test_per_unit_flag_override_changes_only_that_unit(self) -> None:
        p = project(alpha=compiler("gcc", ("-O1", "-G0")))
        self.assertIn("-O1", self.steps(p, "alpha")[1])
        self.assertNotIn("-O1", self.steps(p, "beta")[1])
        self.assertIn("-O2", self.steps(p, "beta")[1])

    def test_per_unit_compiler_override_changes_the_kind(self) -> None:
        p = project(alpha=compiler("ido", ("-O2",)))
        self.assertEqual(len(self.steps(p, "alpha")), 2)
        self.assertEqual(len(self.steps(p, "beta")), 3)

    def test_argv_is_deterministic(self) -> None:
        p = project()
        self.assertEqual(self.steps(p), self.steps(p))
