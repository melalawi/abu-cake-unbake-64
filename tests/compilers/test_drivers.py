"""Compiler driver commands per kind and per-unit override (the one source for Makefile rules and the runner)."""

import json
from dataclasses import replace
from pathlib import Path

from tests.project_fixture import ProjectCase
from unbake import buildfiles
from unbake.compilers import drivers, registry
from unbake.config import Compiler, Held

TOOLS = drivers.Tools("/bin/cpp", "/bin/as", "/bin/n64link")


class DriverTests(ProjectCase):
    versions = ("us",)

    def setUp(self) -> None:
        super().setUp()
        tools = self.project.tools
        sn64 = Compiler("gcc-2.8.1-sn64", "gnu", tools / "sn/cc1", tools / "sn/as", ("-O2", "-G0"), tools / "x")
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
        self.assertEqual((steps.kind, steps.preprocess[0]), ("gnu", "/bin/cpp"))
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
            drivers.templates("gnu")["compile"] or (), {"cc": ("$(CC)",), "codegen": ("$(CG)",), "name": ("$*",)}
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

    def test_actual_vr4300_ido_commands_apply_multiply_policy_before_ordered_caller_flags(self):
        fixture = Path(__file__).parent / "fixtures" / "vr4300-multiply"
        for record in json.loads((fixture / "ordered-argv.json").read_text()):
            with self.subTest(compiler=record["compiler"]):
                original = record["compile_argv_ordered_relative_executable"]
                before = record["preprocess_argv_ordered"]
                steps = drivers.from_flags(
                    record["compiler"],
                    original[0],
                    tuple(record["effective_flags_ordered"]),
                    (),
                    (),
                    "func_80114970_us",
                    before[-1],
                    TOOLS,
                )
                self.assertEqual(steps.compile, (original[0], "-Wab,-r4300_mul", *original[1:]))
                self.assertEqual(steps.preprocess, tuple(before))
                self.assertIsNone(steps.assemble)
                duplicate = drivers.from_flags(
                    record["compiler"],
                    original[0],
                    (*record["effective_flags_ordered"], "-O1", "-O2", "-O1"),
                    (),
                    (),
                    "func_80114970_us",
                    before[-1],
                    TOOLS,
                )
                self.assertEqual(duplicate.compile[1], "-Wab,-r4300_mul")
                self.assertEqual(duplicate.compile[-7:-4], ("-O1", "-O2", "-O1"))
                made = drivers.render(
                    drivers.templates("ido")["compile"] or (),
                    {"cc": ("$(CC)",), "codegen": ("$(CG)",), "name": ("$*",)},
                )
                self.assertEqual(made, ("$(CC)", "-Wab,-r4300_mul", "$(CG)", "-c", "$*.i", "-o", "$*.o"))

    def test_actual_gnu_loop_options_survive_ordinary_make_and_driver_recipes(self):
        # The native-exact 2916-byte case uses the SN64 baseline plus these
        # options; ordinary units_mk previously refused the first option.
        flags = ("-fno-thread-jumps", "-fno-rerun-cse-after-loop")
        spec = registry.specification("gcc-2.8.1-sn64")
        compiler = replace(self.project.compilers[spec.id], cflags=spec.cflags)
        self.project = replace(
            self.project,
            compilers={**self.project.compilers, spec.id: compiler},
            unit_flags={"beta": flags},
        )
        (self.project.src / "beta.c").write_text("int beta(void) { return 0; }\n")
        made = buildfiles.units_mk(self.project)
        self.assertIn("UNIT_CODEGEN := " + " ".join(flags) + "\n", made)
        steps = self.steps("beta")
        self.assertEqual(steps.compile, ("tools/sn/cc1", "-quiet", *spec.cflags, *flags, "beta.i", "-o", "beta.s"))
        for flag in flags:
            self.assertNotIn(flag, steps.preprocess)
        self.assertIn("$(CODEGEN) $(UNIT_CODEGEN)", buildfiles._kind_recipes("gnu"))

    def test_registered_gnu_profiles_include_both_loop_options(self):
        for ident in ("gcc-2.7.2-kmc", "gcc-2.8.1-sn64"):
            with self.subTest(compiler=ident):
                spec = registry.specification(ident)
                for flag in ("-fno-thread-jumps", "-fno-rerun-cse-after-loop"):
                    self.assertIn([flag], registry._read(registry.REGISTRY_PATH)["compilers"][ident]["flag_variants"])
                    _, codegen = drivers.stage_flags(ident, [*spec.cflags, flag])
                    self.assertEqual(codegen, (*spec.cflags, flag))

    def test_gnu_loop_options_remain_refused_by_ido_and_unknown_options_by_gnu(self):
        for ident, flags in (
            ("ido-7.1", ["-fno-thread-jumps"]),
            ("ido-5.3", ["-fno-rerun-cse-after-loop"]),
            ("gcc-2.8.1-sn64", ["-fno-thread-jumps", "-fno-invented-loop-pass"]),
        ):
            with self.subTest(compiler=ident, flags=flags), self.assertRaisesRegex(Held, flags[-1]):
                drivers.stage_flags(ident, flags)
