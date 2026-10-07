"""Explicit compare boundary with real words/dumps and counted native stages."""

import argparse
import hashlib
import struct
from contextlib import contextmanager
from dataclasses import replace
from unittest.mock import patch

from tests.compilers.test_dump_facts import FIXTURES, dumps, measurement, payload
from tests.project_fixture import ProjectCase
from unbake.cli import compare as compare_cli
from unbake.compilers import registry
from unbake.compilers.families.gcc import Gcc
from unbake.config import Compiler
from unbake.work import compare, compare_facts


class CompareDumpTests(ProjectCase):
    versions = ("us-rev1",)

    def setup_trial(self, *, compiler="gcc-2.8.1-sn64", label="baseline"):
        source = self.root / "alpha.c"
        source.write_text((FIXTURES / "A4134-before.i").read_text().replace("func_802A4134_de", "alpha"))
        spec = registry.specification(compiler)
        selected = Compiler(
            spec.id,
            spec.kind,
            self.project.tools / spec.id / spec.cc,
            self.root / spec.as_,
            spec.cflags,
            self.project.tools / "compilers.sha256",
        )
        self.project = replace(
            self.project, compilers={spec.id: selected}, default_compiler=spec.id, unit_flags={"alpha": ("-O1",)}
        )
        trial = compare.Compared(
            "alpha",
            source,
            hashlib.sha256(source.read_bytes()).hexdigest(),
            {"us-rev1": measurement(payload(label))},
            compiler=spec.id,
        )
        return source, trial

    @contextmanager
    def boundary(self, source, trial, *, changed=False, missing=()):
        self.calls = []

        @contextmanager
        def view(project, *_args):
            yield project

        @contextmanager
        def namespace(project, *_args):
            yield project, source

        def preprocess(*args, **kwargs):
            self.calls.append(("preprocess", args[1]))
            return source.read_text()

        def native(argv, cwd, phase, **kwargs):
            stage = "compile" if argv[0] == str(self.project.compiler_for("alpha").cc) else "assemble"
            self.calls.append((stage, argv))
            if stage == "compile":
                for suffix, text in dumps("A4134-before").items():
                    if suffix not in missing:
                        (cwd / f"alpha.i.{suffix}").write_text(text.replace("func_802A4134_de", "alpha"))
            return ""

        def linked(*args):
            self.calls.append(("link", str(args[2])))
            words = list(trial.compares["us-rev1"].candidate)
            if changed:
                words[0] ^= 1
            return struct.pack(f">{len(words)}I", *words), []

        with (
            patch("unbake.fold.provider_reuse.view", view),
            patch("unbake.typemap.namespace.comparison_view", namespace),
            patch("unbake.compilers.drivers.run_preprocess", preprocess),
            patch("unbake.process.run_tool", native),
            patch("unbake.runner.link_function", linked),
            patch.object(compare, "measure", return_value=trial),
            patch("unbake.compilers.candidates.resolve", return_value=(None, trial)),
            patch("unbake.work.attempts.producer_operation", return_value="fixture"),
        ):
            yield

    def test_normal_public_compare_runs_zero_dump_compiles(self):
        source, trial = self.setup_trial()
        with self.boundary(source, trial):
            measured = compare.compare(self.project, self.host, source)
        self.assertEqual(self.calls, [])
        self.assertEqual(measured.facts["us-rev1"]["work_counts"]["dump_compiles"], 0)
        self.assertTrue(all("compiler_facts" not in r for r in measured.facts["us-rev1"]["regions"]))

    def test_explicit_public_compare_runs_one_chosen_dump_recipe_and_reads_once(self):
        source, trial = self.setup_trial()
        real = Gcc.compiler_facts
        with (
            self.boundary(source, trial),
            patch.object(Gcc, "compiler_facts", autospec=True, side_effect=real) as parses,
        ):
            measured = compare.compare(self.project, self.host, source, explain_schedule=True)
        self.assertEqual([stage for stage, _ in self.calls], ["preprocess", "compile", "assemble", "link"])
        argv = self.calls[1][1]
        self.assertIn("-O1", argv)
        self.assertIn("-quiet", argv)
        for flag in ("-ds", "-dS", "-dR", "-dl", "-dg", "-g"):
            self.assertIn(flag, argv)
        self.assertEqual(parses.call_count, 1)
        counts = measured.facts["us-rev1"]["work_counts"]
        self.assertEqual(
            (
                counts["dump_compiles"],
                counts["dump_preprocesses"],
                counts["dump_assembles"],
                counts["dump_files_read"],
                counts["dump_parses"],
            ),
            (1, 1, 1, 5, 1),
        )
        self.assertEqual(
            counts["dump_bytes_read"],
            sum(len(t.replace("func_802A4134_de", "alpha").encode()) for t in dumps("A4134-before").values()),
        )
        self.assertEqual(measured.compares["us-rev1"].target_words_different, 2)
        self.assertTrue(
            any(r.get("compiler_facts", {}).get("search_options") for r in measured.facts["us-rev1"]["regions"])
        )

    def test_exact_explicit_compare_runs_no_dump_recipe(self):
        source, trial = self.setup_trial(label="B-equalize-scheduler-births")
        with self.boundary(source, trial):
            measured = compare.compare(self.project, self.host, source, explain_schedule=True)
        self.assertEqual(self.calls, [])
        self.assertEqual(measured.facts["us-rev1"]["work_counts"]["dump_compiles"], 0)

    def test_ido_explicit_request_is_unsupported_without_native_work(self):
        source, trial = self.setup_trial(compiler="ido-7.1")
        with self.boundary(source, trial):
            measured = compare.compare(self.project, self.host, source, explain_schedule=True)
        self.assertEqual(self.calls, [])
        facts = [r["compiler_facts"] for r in measured.facts["us-rev1"]["regions"] if "compiler_facts" in r]
        self.assertTrue(facts)
        self.assertTrue(all(not f["available"] and "unsupported" in f["reason"] for f in facts))

    def test_diagnostic_byte_change_is_unavailable_and_never_replaces_score(self):
        source, trial = self.setup_trial()
        with self.boundary(source, trial, changed=True):
            measured = compare.compare(self.project, self.host, source, explain_schedule=True)
        self.assertEqual(measured.compares["us-rev1"].target_words_different, 2)
        data = measured.facts["us-rev1"]
        self.assertEqual(data["work_counts"]["dump_files_read"], 0)
        self.assertEqual(data["work_counts"]["dump_parses"], 0)
        self.assertTrue(
            all(
                "changed candidate bytes" in r["compiler_facts"]["reason"]
                for r in data["regions"]
                if "compiler_facts" in r
            )
        )

    def test_immediate_only_and_register_regions_are_selected_separately(self):
        from unbake.work import compare_dump
        from unbake.work.score import measure_words

        result = measure_words("us", bytes.fromhex("24020001"), bytes.fromhex("24020002"))
        self.assertEqual(compare_dump.eligible(compare_facts.facts(result, 0, {}), result), [])
        result = measure_words("us", bytes.fromhex("24420002"), bytes.fromhex("24630002"))
        data = compare_facts.facts(result, 0, {})
        self.assertEqual(len(compare_dump.eligible(data, result)), 1)
        parsed = Gcc().compiler_facts(
            dumps("A4134-before"), result.candidate, (FIXTURES / "A4134-before.i").read_text()
        )
        compare_dump.attach_decisions(data, result, parsed)
        facts = data["regions"][0]["compiler_facts"]
        self.assertEqual(facts["registers"][0]["target_pseudo"], "unavailable")
        self.assertTrue(facts["registers"][0]["pseudos"])

    def test_cli_accepts_explicit_flag(self):
        parser = argparse.ArgumentParser()
        compare_cli.register(parser)
        args = parser.parse_args(["alpha.c", "--explain-schedule"])
        self.assertTrue(args.explain_schedule)
        self.assertFalse(parser.parse_args(["alpha.c"]).explain_schedule)

    def test_real_fp_destination_difference_maps_f24_to_f20_and_conflicts(self):
        from unbake.work import compare_dump
        from unbake.work.score import measure_words

        # neg.s f24,f22 versus neg.s f20,f22, from the retained late negation.
        candidate = (17 << 26) | (16 << 21) | (22 << 11) | (24 << 6) | 7
        target = (candidate & ~(31 << 6)) | (20 << 6)
        result = measure_words("us", struct.pack(">I", target), struct.pack(">I", candidate))
        self.assertEqual(result.typed["register"], 1)
        data = compare_facts.facts(result, 0, {})
        self.assertEqual(len(compare_dump.eligible(data, result)), 1)
        parsed = Gcc().compiler_facts(dumps("A8A8-before"), result.candidate, "")
        compare_dump.attach_decisions(data, result, parsed)
        row = data["regions"][0]["compiler_facts"]["registers"][0]
        self.assertEqual((row["target_hard"], row["candidate_hard"]), (52, 56))
        self.assertEqual(row["pseudos"][0]["pseudo"], 80)
        self.assertIn(52, row["pseudos"][0]["hard_conflicts"])
