"""Registry extension and equivalent native diagnostic paths on real compiler payloads."""

from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from tests.project_fixture import ProjectCase
from unbake.compilers import registry
from unbake.compilers.families import Family, family_for
from unbake.config import Compiler

FIXTURES = Path(__file__).parent / "fixtures"
SOURCE = Path("tests/fixtures/battletanx_float_abi/func_8010AA6C_us.c").read_text()


class FamilyContractTests(ProjectCase):
    versions = ("us",)

    def selected(self, ident):
        spec = registry.specification(ident)
        compiler = Compiler(
            spec.id,
            spec.kind,
            self.project.tools / spec.id / spec.cc,
            Path(spec.as_),
            spec.cflags,
            self.project.tools / "compilers.sha256",
        )
        self.project = replace(self.project, compilers={spec.id: compiler}, default_compiler=spec.id)
        source = self.project.src / "func_8010AA6C_us.c"
        source.write_text(SOURCE)
        return spec, source

    def test_registry_only_extension_reuses_complete_family_protocol(self):
        specs = registry.registry()
        for ident in specs:
            with self.subTest(compiler=ident):
                original = specs[ident]
                extra = replace(original, id="test-extension")
                with patch.object(registry, "registry", return_value={**specs, extra.id: extra}):
                    family = family_for(extra.id)
                    self.assertIsInstance(family, Family)
                    self.assertEqual(family.native_templates(), family_for(ident).native_templates())
                    self.assertEqual(
                        family.shape(extra.id, extra.cflags), family_for(ident).shape(ident, original.cflags)
                    )
                    self.assertEqual(registry.specification(extra.id).m2c, original.m2c)

    def test_shared_driver_kind_keeps_each_compiler_preprocessing_semantics(self):
        import types

        from unbake.compilers import drivers
        from unbake.compilers.families.gcc import Gcc

        class Alternate(Gcc):
            def preprocess_flags(self, preprocess, codegen):
                return (*super().preprocess_flags(preprocess, codegen), "-DADAPTER=1")

        original = registry.specification("gcc-2.8.1-sn64")
        extra = replace(original, id="test-extension", family="alternate")
        specs = {**registry.registry(), extra.id: extra}
        real_import = __import__("importlib").import_module

        def module(name):
            return types.SimpleNamespace(adapter=Alternate) if name.endswith(".alternate") else real_import(name)

        with (
            patch.object(registry, "registry", return_value=specs),
            patch("importlib.import_module", side_effect=module),
        ):
            self.assertIn("-DADAPTER=1", drivers.stage_flags(extra.id, ["-O2"])[0])
            self.assertNotIn("-DADAPTER=1", drivers.stage_flags(original.id, ["-O2"])[0])
            self.assertEqual(drivers.templates(extra.kind), drivers.templates(original.kind))

    def test_real_native_allocation_production_uses_effective_family_paths_and_work_counts(self):
        for ident, first in (("gcc-2.7.2-kmc", 90), ("gcc-2.8.1-sn64", 98), ("ido-7.1", None)):
            with self.subTest(compiler=ident):
                _spec, source = self.selected(ident)
                family = family_for(ident)
                work = self.root / ident
                work.mkdir()
                calls = []

                def native(argv, cwd, phase, calls=calls, first=first, work=work, ident=ident, **kwargs):
                    calls.append(argv)
                    if argv[0] == str(self.host.cpp):
                        return SOURCE
                    if first is None:
                        (work / "source.s").write_text((FIXTURES / "ido-7.1-assignments.s").read_text())
                    else:
                        for suffix in ("lreg", "greg"):
                            (work / ("source.i." + suffix)).write_text(
                                (FIXTURES / "diagnostics" / f"{ident}.{suffix}").read_text()
                            )
                    return ""

                with patch("unbake.process.run_tool", side_effect=native):
                    result = family.collect_allocation(self.project, self.host, source, "us", work)
                self.assertEqual(len(calls), 1 if first is None else 2)
                self.assertEqual(calls[-1][0], str(self.project.compiler_for(source).cc))
                for flag in family.dump_flags():
                    self.assertIn(flag, calls[-1])
                if first is None:
                    self.assertIn("-K", calls[0])
                    self.assertFalse(result.pseudos)
                    self.assertTrue({4, 5, 6, 7, 31} <= set(result.hard_registers))
                    self.assertFalse(family.schedule_available())
                    self.assertFalse(family.schedule(None).available)
                else:
                    ranked = sorted((p for p in result.pseudos if p.rank is not None), key=lambda p: p.rank)
                    self.assertEqual((len(ranked), ranked[0].number, ranked[0].words), (18, first, 2))
                    self.assertEqual(calls[0][0], str(self.host.cpp))
                    self.assertIn("-g", calls[-1])

    def test_real_scheduling_production_and_unsupported_family_skip(self):
        for ident in ("gcc-2.7.2-kmc", "gcc-2.8.1-sn64", "ido-7.1"):
            _spec, source = self.selected(ident)
            family = family_for(ident)
            work = self.root / ident
            work.mkdir()
            calls = []

            def native(argv, cwd, phase, calls=calls, work=work, ident=ident, **kwargs):
                calls.append(argv)
                if argv[0] == str(self.host.cpp):
                    return SOURCE
                for suffix in ("sched2", "dbr"):
                    (work / ("source.i." + suffix)).write_text(
                        (FIXTURES / "diagnostics" / f"{ident}.{suffix}").read_text()
                    )
                return ""

            with patch("unbake.process.run_tool", side_effect=native):
                result = family.collect_schedule(self.project, self.host, source, "us", work)
            self.assertEqual(len(calls), 2 if family.schedule_available() else 0)
            self.assertEqual(result.available, family.schedule_available())
            if result.available:
                self.assertGreater(len(result.sched2), 4)
                self.assertGreater(len(result.dbr), 4)
