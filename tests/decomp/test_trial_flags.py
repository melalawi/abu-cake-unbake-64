"""Compiler-owned flag probes and named refusals for incomplete registry entries."""

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from tests.decomp.support import fixture
from unbake.decomp.trial_flags import compiler_variants, variant_project
from unbake.project import toolchain
from unbake.project.config import Held


class CompilerFlagTests(unittest.TestCase):
    def test_source_compiler_override_selects_registry_variants(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project, _, source = fixture(root)
            baseline = project.compiler_for(source)
            gcc = replace(baseline, id="gcc-2.7.2-kmc", kind="sn64", cflags=("-O2",))
            project = replace(project, compilers={baseline.id: baseline, gcc.id: gcc}, units={source.stem: gcc.id})
            registry = root / "compilers.toml"
            registry.write_text('[compilers."gcc-2.7.2-kmc"]\nflag_variants=[[], ["-O1"]]\n')
            # No game config exists: the selected source compiler is sufficient.
            with patch.object(toolchain, "REGISTRY_PATH", registry):
                self.assertEqual(compiler_variants(project, source), [(), ("-O1",)])
                configured = variant_project(project, source, ("-O1",))
                self.assertEqual(configured.compiler_for(source).cflags, ("-O2", "-O1"))
                self.assertEqual(configured.compilers[baseline.id], baseline)
                self.assertEqual(project.compiler_for(source).cflags, ("-O2",))

    def test_missing_or_malformed_variants_refuse_by_compiler_id(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project, _, source = fixture(root)
            registry = root / "compilers.toml"
            invalid: tuple[object, ...] = (None, "-O1", [], [["-O1"]], [[], []], [[], [1]], [[], [""]], [[], [" "]])
            with patch.object(toolchain, "REGISTRY_PATH", registry):
                for value in invalid:
                    with self.subTest(value=value):
                        registry.write_text(
                            '[compilers."ido-7.1"]\n'
                            + ("" if value is None else "flag_variants=" + json.dumps(value) + "\n")
                        )
                        with self.assertRaises(Held) as raised:
                            compiler_variants(project, source)
                        self.assertEqual(raised.exception.phase, "try")
                        self.assertIn("[compilers.ido-7.1].flag_variants", raised.exception.reason)
                for content in ('[compilers."other"]\nflag_variants=[[]]\n', "malformed [", "compilers=[]\n"):
                    registry.write_text(content)
                    with self.assertRaisesRegex(Held, "ido-7.1"):
                        compiler_variants(project, source)
                registry.unlink()
                with self.assertRaisesRegex(Held, "ido-7.1"):
                    compiler_variants(project, source)

    def test_every_registered_compiler_has_explicit_baseline_and_unique_variants(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project, _, source = fixture(Path(temporary))
            baseline = project.compiler_for(source)
            for ident in toolchain.registry():
                with self.subTest(compiler=ident):
                    compiler = replace(baseline, id=ident)
                    selected = replace(project, compilers={ident: compiler}, default_compiler=ident)
                    variants = compiler_variants(selected, source)
                    self.assertEqual(variants[0], ())
                    self.assertGreater(len(variants), 1)
                    self.assertEqual(len(variants), len(set(variants)))
