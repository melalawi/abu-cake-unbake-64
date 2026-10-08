"""Retained pass records and permuter logs; unavailable internal work stays null."""

import subprocess
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tests.compilers.test_dump_facts import FIXTURES, dumps, payload
from tests.kit import TempCase
from tests.unified.support import B, gcc_project, retained
from unbake import process
from unbake.compilers.families.gcc import Gcc
from unbake.compilers.families.gcc.lineage import lineage
from unbake.compilers.families.types import Allocation
from unbake.compilers.options import classify_capability
from unbake.config import Held
from unbake.decomp import explain
from unbake.search import permute
from unbake.work.score import measure_words


class LineageTelemetryTests(TempCase):
    def test_actual_gcc_pass_lineage_keeps_bounded_def_use_origins_and_unknown_target_rtl(self):
        doc = payload()
        parsed = Gcc().compiler_facts(
            dumps("A4134-before"), tuple(doc["candidate"]), (FIXTURES / "A4134-before.i").read_text()
        )
        result = lineage(dumps("A4134-before"), parsed["instructions"])
        self.assertTrue(result["available"])
        self.assertTrue(result["artifacts"])
        self.assertTrue(all(len(row["definitions"]) <= 5 for row in result["slices"]))
        self.assertTrue(all(row["unavailable_target_rtl"] for row in result["slices"]))
        self.assertTrue(any(row["source_text"] for row in result["slices"]))
        missing = lineage({}, {})
        self.assertFalse(missing["available"])
        self.assertIn("rtl", missing["missing"])

    def test_diagnostic_link_mismatch_withholds_register_authority(self):
        comparison = measure_words("us", retained("bt-target.bin"), retained("bt-baseline.bin"))
        allocation = Allocation(
            (), (), (), (), family="gcc", instruction_origins=((4, 134, (74,)),), linked_sha256="a" * 64
        )
        observed = explain.align(allocation, comparison)
        self.assertEqual(observed.differences, ())
        self.assertEqual(observed.instruction_origins, ())
        self.assertIn("diagnostic recipe has no equal linked-byte proof", observed.limitations)

    def test_counterfactual_native_compiler_rejection_keeps_first_streams_argv_and_capability_state(self):
        argv = ["cc1", "-unknown-option", "retained.i"]
        with (
            patch.object(
                process.subprocess,
                "run",
                return_value=subprocess.CompletedProcess(argv, 1, "", "cc1: unrecognized option -unknown-option\n"),
            ),
            self.assertRaises(Held) as caught,
        ):
            process.run_tool(argv, self.root, "compile", context={"stage": "compile", "compiler": "gcc-2.8.1-sn64"})
        capability = classify_capability("a" * 64, fault=caught.exception.fault.document())
        self.assertEqual(capability.state, "compiler_refused")
        self.assertEqual(capability.argv, tuple(argv))
        native = process.native_results(process.Fault.read(capability.native))[0]
        self.assertIn("unrecognized", native.stderr)

    def test_counterfactual_no_export_wrapper_log_keeps_actual_wrapper_counts_and_nullable_internal_effort(self):
        project, host, file = gcc_project(self.root, content="int func_800B1520_us(void) { return 1; }\n")
        obj = self.root / "target.o"
        obj.write_bytes(retained("bt-exact.o"))
        generator = permute.Permuter("us", obj, 30)
        ctx = SimpleNamespace(
            project=project, policy=host, out=self.root / "permuter", source=file, deadline=time.monotonic() + 30
        )
        trial = SimpleNamespace(
            function=B, compares={"us": measure_words("us", retained("bt-target.bin"), retained("bt-baseline.bin"))}
        )
        # The actual captured run exported nothing; wrapper events below are
        # labeled counterfactual telemetry on that no-export public boundary.
        log = (Path(__file__).parents[1] / "fixtures/creative_slim/README.txt").read_text()
        self.assertTrue(log)

        def run(command, work, environment, budget, path):
            path.write_text("base score = 45\n")
            (work / "compile-events.log").write_text(
                "start\nfailure\nstart\n" + "a" * 64 + " output.o\nstart\n" + "a" * 64 + " duplicate.o\n"
            )
            return permute._RunResult(True, 0, "exit")

        with (
            patch.object(permute.toolchain, "verify"),
            patch.object(permute, "checkout", side_effect=lambda archive, digest, work: work / "main.py"),
            patch.object(permute, "_run", side_effect=run),
        ):
            self.assertEqual(list(generator.propose(file.read_text(), trial, ctx)), [])
        self.assertEqual(
            {k: generator.telemetry[k] for k in ("compiles", "failures", "duplicate_outputs", "exports")},
            {"compiles": 3, "failures": 1, "duplicate_outputs": 1, "exports": 0},
        )
        for key in ("generated", "attempted", "internal_improvements", "strict_confirmations"):
            self.assertIsNone(generator.telemetry[key])
        self.assertTrue(generator.telemetry["unknown_reason"])
        self.assertEqual(generator.telemetry["stop_reason"], "exit")
