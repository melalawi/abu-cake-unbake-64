"""Compiler driver commands per kind and per-unit override (the one source for Makefile rules and the runner)."""

from dataclasses import replace

from tests.project_fixture import ProjectCase
from unbake.compilers import drivers
from unbake.config import Compiler

TOOLS = drivers.Tools("/bin/cpp", "/bin/as", "/bin/n64link")


class DriverTests(ProjectCase):
    versions = ("us",)

    def setUp(self) -> None:
        super().setUp()
        tools = self.project.tools
        sn64 = Compiler("gcc-2.8.1-sn", "sn64", tools / "sn/cc1", tools / "sn/as", ("-O2", "-G0"), tools / "x")
        self.project = replace(
            self.project,
            compilers={**self.project.compilers, sn64.id: sn64},
            cppflags=("-DFOO",),
            units={"beta": sn64.id},
            unit_flags={"gamma": ("-O1", "-DGAMMA")},
        )

    def steps(self, unit: str) -> drivers.Steps:
        return drivers.steps(self.project, "us", unit, f"src/{unit}.c", TOOLS)

    def test_ido_preprocesses_with_its_own_cc_and_never_assembles(self) -> None:
        steps = self.steps("alpha")
        self.assertEqual(steps.kind, "ido")
        self.assertEqual(steps.preprocess[0], "tools/ido-7.1/cc")
        self.assertIn("-E", steps.preprocess)
        self.assertIn("-DVERSION_US", steps.preprocess)
        self.assertIn("-Iinclude", steps.preprocess)
        self.assertEqual(steps.compile[-4:], ("-c", "alpha.i", "-o", "alpha.o"))
        self.assertIsNone(steps.assemble)

    def test_sn64_uses_host_cpp_and_assembles_through_n64link(self) -> None:
        steps = self.steps("beta")
        self.assertEqual((steps.kind, steps.preprocess[0]), ("sn64", "/bin/cpp"))
        self.assertIn("-DFOO", steps.preprocess)
        self.assertIn("-quiet", steps.compile)
        assert steps.assemble is not None
        self.assertEqual(steps.assemble[:4], ("/bin/n64link", "asn64", "--as", "/bin/as"))
        self.assertIn("-march=vr4300", steps.assemble)

    def test_unit_flags_change_only_that_unit_and_keep_compiler_flags_first(self) -> None:
        gamma, alpha = self.steps("gamma"), self.steps("alpha")
        self.assertEqual([f for f in gamma.compile if f in ("-O2", "-O1")], ["-O2", "-O1"])
        self.assertIn("-DGAMMA", gamma.preprocess)
        self.assertNotIn("-O1", alpha.compile)
        self.assertNotIn("-DGAMMA", alpha.preprocess)

    def test_runner_and_makefile_render_the_same_template(self) -> None:
        self.assertEqual(self.steps("beta"), self.steps("beta"))
        made = drivers.render(
            drivers.TEMPLATES["sn64"]["compile"] or (), {"cc": ("$(CC)",), "codegen": ("$(CG)",), "name": ("$*",)}
        )
        self.assertEqual(made, ("$(CC)", "-quiet", "$(CG)", "$*.i", "-o", "$*.s"))

    def test_stage_contract_keeps_macro_options_and_strips_only_ido_codegen_optimization(self):
        self.project = replace(
            self.project, unit_flags={"alpha": ("-O3", "-imacros", "flags.h", "-iquote", "quotes", "-DVALUE=7")}
        )
        steps = self.steps("alpha")
        self.assertNotIn("-O2", steps.preprocess)
        self.assertNotIn("-O3", steps.preprocess)
        self.assertIn("-O3", steps.compile)
        for option, value in (("-imacros", "flags.h"), ("-iquote", "quotes")):
            index = steps.preprocess.index(option)
            self.assertEqual(steps.preprocess[index + 1], value)
        context = drivers.preprocess_command(
            self.project, "/bin/cpp", "us", "alpha", self.project.src / "alpha.c", non_matching=False
        )
        self.assertEqual(context[1:], [*steps.preprocess[1:-1], str(self.project.src / "alpha.c")])

    def test_malformed_missing_or_unsupported_flags_fail_by_name(self):
        from unbake.config import Held

        for flags, bad in [
            (("-imacros",), "-imacros"),
            (("-iquote", "-DVALUE"), "-iquote"),
            (("-mfp16",), "-mfp16"),
            (("-invented",), "-invented"),
        ]:
            self.project = replace(self.project, unit_flags={"alpha": flags})
            with self.assertRaises(Held) as caught:
                self.steps("alpha")
            self.assertIn(bad, caught.exception.reason)
