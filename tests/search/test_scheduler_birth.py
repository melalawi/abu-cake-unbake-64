"""Proven mapped move, conservative holds, and all-version byte acceptance."""

import hashlib
import json
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

from tests.compilers.test_dump_facts import FIXTURES, dumps, measurement, payload
from tests.project_fixture import ProjectCase
from unbake import cdecl
from unbake.compilers.families.gcc import Gcc
from unbake.compilers.families.types import Allocation
from unbake.search import core, methods
from unbake.work import compare, compare_facts


def evidence(source=None):
    source = source or (FIXTURES / "A4134-before.i").read_text()
    result = measurement(payload())
    data = compare_facts.facts(result, 0x802A4134, {})
    from unbake.work.compare_dump import attach_decisions

    parsed = Gcc().compiler_facts(dumps("A4134-before"), result.candidate, source)
    attach_decisions(data, result, parsed)
    return data


class SchedulerBirthTests(unittest.TestCase):
    def propose(self, source, data):
        generator = methods("scheduler-birth")[0]
        ctx = core.Context(None, None, None, None, Allocation((), (), (), ()), (), float("inf"), data)
        trial = SimpleNamespace(function="func_802A4134_de")
        return list(generator.propose(source, trial, ctx))

    def test_actual_source_gets_one_c89_local_and_only_mapped_consumer_changes(self):
        source = (FIXTURES / "A4134-before.i").read_text()
        variants = self.propose(source, evidence())
        self.assertEqual(len(variants), 1)
        text = variants[0].source
        self.assertIn("s32 schedule_value;", text)
        self.assertIn("schedule_value = value + 2;", text)
        self.assertIn("*selected = reflected - schedule_value;", text)
        self.assertIn("return *selected;", text)
        self.assertNotIn("value += 2;", text)
        self.assertLess(text.index("s32 schedule_value;"), text.index("s32 reflected ="))
        cdecl.parse(text)

    def test_no_evidence_ambiguous_pair_or_winner_gives_no_move(self):
        source = (FIXTURES / "A4134-before.i").read_text()
        self.assertEqual(self.propose(source, {}), [])
        data = evidence()
        region = next(r for r in data["regions"] if r.get("compiler_facts", {}).get("search_options"))
        region["compiler_facts"]["search_options"] *= 2
        self.assertEqual(self.propose(source, data), [])

    def test_unsafe_fp_live_out_address_taken_side_effect_and_shadow_sources_hold(self):
        original = (FIXTURES / "A4134-before.i").read_text()
        bad_sources = [
            original.replace("s32 reflected = *frame_count * 2;", "f32 reflected = *frame_count * 2;"),
            original.replace("return *selected;", "return value;"),
            original.replace("s32 value;", "s32 value; s32 *escape = &value;"),
            original.replace("s32 value;", "volatile s32 value;"),
            original.replace("*frame_count * 2;", "func_802744D4_de() * 2;"),
            original.replace("*frame_count * 2;", "*frame_count++ * 2;"),
            original.replace("value += 2;", "s32 value;\n            value += 2;"),
            original.replace("value += 2;", "value += 3;"),
            original.replace("s32 *frame_count", "f32 *frame_count"),
        ]
        for index, source in enumerate(bad_sources):
            data = evidence()
            option = next(o for r in data["regions"] for o in r.get("compiler_facts", {}).get("search_options", []))
            # Retain mapping but use changed source expression: AST safety must reject it.
            option["birth_expression"] = next(line.strip() for line in source.splitlines() if "reflected =" in line)
            option["update_expression"] = next(line.strip() for line in source.splitlines() if "value +=" in line)
            with self.subTest(case=index):
                self.assertEqual(self.propose(source, data), [])


class BirthAcceptanceTests(ProjectCase):
    versions = ("de", "eu", "eu-x", "us", "us-rev1")

    def test_byte_comparison_accepts_winner_and_holds_regression_failure_and_unchanged(self):
        generator = methods("scheduler-birth")[0]
        source = self.root / "func_802A4134_de.c"
        initial = (FIXTURES / "A4134-before.i").read_text().replace("return *selected;", "return frame;")
        source.write_text(initial)
        baseline = json.loads((FIXTURES / "A4134-B-end-selected-before-exit.json").read_text())
        winner = json.loads((FIXTURES / "A4134-B-equalize-scheduler-births.json").read_text())
        regression = json.loads((FIXTURES / "A4134-baseline.json").read_text())
        for state in ("winner", "regression", "unchanged", "held"):
            measured_calls = []
            dump_calls = []

            def measure(project, policy, file, *, versions=None, measured_calls=measured_calls, state=state):
                text = file.read_text()
                changed = "schedule_value = value + 2;" in text
                measured_calls.append((changed, versions))
                if changed and state == "held":
                    from unbake.config import Held
                    from unbake.process import named

                    raise Held(named("fixture.compile", "fixture compile held", owner="tests", stage="search"))
                docs = (
                    winner
                    if changed and state == "winner"
                    else regression
                    if changed and state == "regression"
                    else baseline
                )
                results = {v: measurement(doc, v) for v, doc in docs.items() if versions is None or v in versions}
                return compare.Compared(
                    file.stem, file, hashlib.sha256(text.encode()).hexdigest(), results, compiler="gcc-2.8.1-sn64"
                )

            def attach(project, trial):
                trial.facts = {v: compare_facts.facts(result, 0x802A4134, {}) for v, result in trial.compares.items()}

            def collect(project, host, trial, dump_calls=dump_calls):
                from unbake.work.compare_dump import attach_decisions

                dump_calls.append(trial.source_sha256)
                for v, result in trial.compares.items():
                    attach_decisions(
                        trial.facts[v], result, Gcc().compiler_facts(dumps("A4134-before"), result.candidate, initial)
                    )

            out = self.root / state
            with (
                patch.object(core, "measure", side_effect=measure),
                patch.object(core, "preprocess", return_value=initial),
                patch.object(core.explain, "allocation") as old_dumps,
                patch("unbake.work.compare_facts.attach", side_effect=attach),
                patch("unbake.work.compare_dump.collect", side_effect=collect),
            ):
                policy = replace(self.host, values={**self.host.values, "search": {"beam": 1, "stall_trials": 1}})
                found = core.run(self.project, policy, source, [generator], out, 30)
            with self.subTest(state=state):
                self.assertEqual(found.trials, 2)
                self.assertEqual(len(dump_calls), 1)
                self.assertEqual(old_dumps.call_count, 0)
                self.assertEqual(len(measured_calls), 3 if state == "winner" else 2)
                self.assertEqual(found.trial.exact, state == "winner")
                self.assertEqual(set(found.trial.compares), set(self.versions))
                self.assertEqual(found.source.read_text() != initial, state == "winner")
                rows = [json.loads(line) for line in found.steps.read_text().splitlines()]
                self.assertEqual(len(rows), 2 if state == "winner" else 3)
                self.assertEqual(sum(row["cached"] for row in rows), 0 if state == "winner" else 1)
                self.assertEqual(rows[-1]["confirmed"], state == "winner")


if __name__ == "__main__":
    unittest.main()
