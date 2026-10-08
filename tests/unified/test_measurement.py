"""Linked real BT extents, strict acceptance, current readback and public compare replay."""

import argparse
import hashlib
import io
from pathlib import Path
from unittest.mock import patch

from tests.kit import TempCase
from tests.unified.support import B, gcc_project, native_replay, retained
from unbake import land
from unbake.cli import compare as compare_cli
from unbake.cli.args import Context
from unbake.compilers.compiler_contracts import validate_preprocessed
from unbake.compilers.options import classify_capability
from unbake.config import Held
from unbake.process import named
from unbake.work import compare, compare_dump
from unbake.work.score import Measurement, measure_words, unavailable


class MeasurementTests(TempCase):
    def test_real_bt_baseline_has_45_aligned_and_771_positional_mismatches(self):
        result = measure_words("us", retained("bt-target.bin"), retained("bt-baseline.bin"))
        self.assertEqual((result.target_words, result.identical_words), (7163, 7118))
        self.assertEqual(result.strict["positional_words"], 771)
        self.assertEqual(
            result.strict["target_sha256"], "d210d533d64c8a00b0c27008332c35693bca1d7c416cbca44b45e2d86b022a93"
        )
        self.assertFalse(result.exact)
        self.assertEqual(result.target_words_different, 45)

    def test_real_exact_extent_without_digest_chain_cannot_accept_or_load_as_current(self):
        result = measure_words("us", retained("bt-target.bin"), retained("bt-exact.bin"))
        self.assertEqual(result.strict["positional_words"], 0)
        self.assertFalse(result.exact)
        document = result.document()
        del document["provenance"]
        with self.assertRaisesRegex(ValueError, "complete M10"):
            Measurement.read(document)
        # Counterfactual nonword tail tests padding cannot establish identity.
        short = measure_words("us", b"\x01", b"\x01\0")
        self.assertNotEqual(short.strict["size_delta"], 0)
        self.assertFalse(short.exact)

    def test_public_compare_retains_full_chain_and_renders_without_diagnostic_jobs(self):
        project, host, file = gcc_project(self.root, content="int func_800B1520_us(void) { return 1; }\n")
        args = argparse.Namespace(
            file=file,
            source_root=[],
            include_root=[],
            recipe=None,
            flags=False,
            require_version=["us"],
            explain_schedule=False,
        )
        ctx = Context("compare", args, project.root, None, io.StringIO(), host)
        calls = []
        with (
            native_replay(project, lambda text, recipe: retained("bt-exact.bin"), calls),
            patch.object(Context, "project", return_value=project),
            patch("unbake.steps.ensure"),
            patch.object(compare_dump, "collect", side_effect=AssertionError("implicit dump")),
        ):
            result = compare_cli.run(ctx)
            self.assertTrue(result.data["required_exact"])
            self.assertTrue(result.data["exact"])
            measurement = Measurement.read(result.data["versions"]["us"])
            self.assertTrue(measurement.provenance_valid)
            self.assertEqual(measurement.strict["target_bytes"], 28652)
            self.assertEqual(len(calls), 1)
            attempt = land.exact_attempt(project, host, B, file, required_versions=("us",))
            self.assertEqual(attempt.sha256, hashlib.sha256(file.read_bytes()).hexdigest())
            measurement.document()
            result.document()
            self.assertEqual(len(calls), 1)

    def test_publication_refuses_changed_target_and_dependency_and_incomplete_required_versions(self):
        project, host, file = gcc_project(self.root, content="int func_800B1520_us(void) { return 1; }\n")
        calls = []
        with native_replay(project, lambda text, recipe: retained("bt-exact.bin"), calls):
            measured = compare.compare(project, host, file, required_versions=("us",))
        self.assertTrue(compare.acceptance(measured.compares, ("us",)))
        self.assertFalse(compare.acceptance(measured.compares, ("us", "eu")))
        self.assertFalse(compare.acceptance(measured.compares, ("us",), ["placement unavailable"]))
        old = project.version("us").baserom.read_bytes()
        project.version("us").baserom.write_bytes(old[:-1] + bytes([old[-1] ^ 1]))
        with self.assertRaises(Held) as held:
            land.exact_attempt(project, host, B, file, required_versions=("us",))
        self.assertEqual(held.exception.key, "land.stale_proof")
        project.version("us").baserom.write_bytes(old)
        (project.include[-1] / "changed.h").write_text("extern int changed;\n")
        file.write_text(file.read_text() + '#include "changed.h"\n')
        with self.assertRaises(Held) as held:
            land.exact_attempt(project, host, B, file)
        self.assertEqual(held.exception.key, "land.not_compared")

    def test_real_active_ido_error_zero_exit_is_rejected_by_shared_standalone_contract(self):
        fixtures = Path(__file__).parents[1] / "compilers/fixtures/active_error"
        for ident in ("ido-5.3", "ido-7.1"):
            folder = fixtures / ident / "active-us"
            with self.subTest(ident=ident), self.assertRaisesRegex(ValueError, "active #error"):
                validate_preprocessed((folder / "stdout").read_text(), (folder / "stderr").read_text(), 0, "ido-1")
        self.assertEqual(validate_preprocessed("int x;\n", "", 0, "gcc-1"), "int x;\n")

    def test_missing_native_measurement_is_unavailable_not_measured_nonexact(self):
        fault = Held(named("compiler.policy", "unsupported option", owner="compilers", stage="options")).fault
        m = unavailable("us", 7163, fault)
        self.assertEqual(classify_capability("a" * 64, m).state, "unavailable")
        self.assertEqual(classify_capability("a" * 64, fault=fault.document()).state, "tool_refused")
        self.assertIsNone(m.percent)
