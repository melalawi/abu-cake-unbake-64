"""Retained real include/provider, processor-resource and compare capture replays.

These checks establish advisory invariants, not semantic calibration or new native bytes.
"""

import hashlib
import json
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from tests.cycle.test_single_writer import SingleWriterTests
from tests.kit import TempCase
from tests.project_fixture import make
from tests.test_resource_build import ResourceBuildTests
from tests.work.test_creative_slim import B, packed, payloads, source
from unbake import cache
from unbake.cycle import ladder
from unbake.cycle.rank import Candidate
from unbake.layout import subsystems as sub
from unbake.project.headers import Graph
from unbake.work import explain, hints, plan
from unbake.work.score import measure_words

FIXTURE = Path(__file__).parent / "fixtures/subsystem_provider_retention"


class AdvisoryCases(TempCase):
    def provider_project(self):
        project, host = make(self.root, versions=("us",))
        manifest = json.loads((FIXTURE / "manifest.json").read_text())
        for row in manifest["payloads"]:
            content = (FIXTURE / row["path"]).read_bytes()
            self.assertEqual(hashlib.sha256(content).hexdigest(), row["sha256"])
            target = project.root / row["path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        # The actual retained source survives; placement is from the retained split's fixture row.
        name = "func_8008001C_us"
        project.version("us").split.write_text(
            "segments:\n  - name: main\n    type: code\n    start: 0x40\n"
            "    vram: 0x8008001C\n    subsegments:\n      - [0x40, c, func_8008001C_us]\n  - [0x100]\n"
        )
        return project, host, name

    def test_actual_retained_providers_remain_distinct_unknown_and_proposals_do_not_expand_selection(self):
        project, _host, name = self.provider_project()
        with patch("subprocess.run", side_effect=AssertionError("no native or preparation")):
            facts = sub.retained(project)
            snapshot = sub.snapshot(project, facts)
            packets = plan.cohorts(
                [Candidate(name, 192, ("us",), True, None)],
                snapshot,
                {"include/common/types_d507c48987bb.h": "provider-owner"},
                owner="consumer-owner",
            )
        providers = [row for row in snapshot.entities if row.kind == "provider"]
        direct = next(row for row in providers if row.path.endswith("types_d507c48987bb.h"))
        self.assertTrue(all(row.state == "unknown" for row in snapshot.memberships))
        self.assertTrue(any(direct.id in row.provider_ids for row in packets))
        self.assertTrue(any(row.ownership_conflicts for row in packets))
        self.assertTrue(all(row.candidate_ids == (name,) for row in packets))
        # The independently guarded provider in the captured rewritten umbrella remains separate.
        graph = Graph.capture(project)
        paths = graph.closure((project.include[0] / "span_1000/code_800F45C8.h",)).paths
        second = project.include[0] / "common/types_0847ae1836e8.h"
        self.assertIn(second, paths)
        self.assertNotEqual(direct.path, str(second.relative_to(project.root)))
        self.assertTrue((project.src / (name + ".c")).read_text().startswith("#include"))

    def test_actual_provider_replay_cold_warm_order_removal_and_corrupt_cache_agree(self):
        project, _host, _name = self.provider_project()
        facts = sub.retained(project)
        cold = sub.snapshot(project, facts)
        reversed_facts = replace(
            facts,
            entities=tuple(reversed(facts.entities)),
            evidence=tuple(reversed(facts.evidence)),
            families=tuple(reversed(facts.families)),
        )
        self.assertEqual(cold.document(), sub.snapshot(project, reversed_facts).document())
        self.assertEqual(cold.document(), sub.aggregate(facts).document())
        path = cache.Cache(project.cache).path("subsystems", cold.evidence_key)
        path.write_bytes(b"broken advisory cache")
        self.assertEqual(cold.document(), sub.snapshot(project, facts).document())
        withdrawn = replace(facts, evidence=())
        changed = sub.snapshot(project, withdrawn)
        self.assertNotEqual(cold.evidence_key, changed.evidence_key)
        self.assertFalse(changed.boundary_edges)
        self.assertTrue(all(row.state == "unknown" for row in changed.memberships))

    def test_actual_chosen_compare_hints_and_explain_do_no_extra_native_work(self):
        project, host, name = self.provider_project()
        snapshot = sub.snapshot(project)
        # Actual chosen compare capture supplies obstacles; labels alone never rescore it.
        captured = payloads(B)
        self.assertTrue(captured)
        obstacle = {"buckets": {"calls": 1}, "capture": captured}
        with (
            patch("subprocess.run", side_effect=AssertionError("no extra native work")),
            patch.object(sub, "snapshot", return_value=snapshot) as lookup,
        ):
            report = explain.explain(project, host, name, ("subsystems", "hints"))
            rendered = hints.for_subject(name, snapshot, obstacle)
        self.assertEqual(lookup.call_count, 1)
        self.assertEqual(rendered[0].snapshot_key, snapshot.evidence_key)
        self.assertEqual(rendered[0].mismatch_refs, ("calls",))
        self.assertTrue(rendered[0].provider_ids)
        self.assertEqual(report.sections["hints"][0]["snapshot_key"], snapshot.evidence_key)
        self.assertEqual(report.sections["subsystems"]["memberships"][0]["state"], "unknown")

    def test_actual_provider_competing_observations_are_mixed_and_correlations_never_double_vote(self):
        project, _host, name = self.provider_project()
        facts = sub.retained(project)
        entity = next(row for row in facts.entities if row.kind == "function")
        source = project.src / (name + ".c")
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        # Counterfactual competing annotations on the actual retained family test uncertainty;
        # they are not claimed as this function's recovered semantics or held-out ground truth.
        annotations = tuple(
            sub.Evidence(
                label,
                "source_semantics",
                (entity.id,),
                "captured source: competing-annotation replay",
                digest,
                label,
                (label,),
                6,
                True,
            )
            for label in ("ui_menus", "physics_math")
        )
        annotated = replace(facts, evidence=(*facts.evidence, *annotations))
        observed = sub.aggregate(annotated).for_subject(name)[0]
        self.assertEqual((observed.state, observed.tier, observed.primary), ("mixed", "medium", None))
        duplicate = replace(annotations[0], id="duplicate decoded/C representation")
        duplicated = sub.aggregate(replace(annotated, evidence=(*annotated.evidence, duplicate))).for_subject(name)[0]
        self.assertEqual(observed.support_by_label, duplicated.support_by_label)
        stale = tuple(replace(row, unresolved_reason="stale correspondence") for row in annotations)
        self.assertEqual(
            sub.aggregate(replace(facts, evidence=(*facts.evidence, *stale))).for_subject(name)[0].state, "unknown"
        )
        contradicted = replace(
            annotated,
            evidence=(*annotated.evidence, replace(annotations[0], id="measured conflict", polarity="conflict")),
        )
        self.assertEqual(sub.aggregate(contradicted).for_subject(name)[0].tier, "low")


class ResourceAdvisoryCases(ResourceBuildTests):
    # Explicit method selection runs this new real boot replay only, never inherited discovery.
    def test_actual_boot_keeps_storage_execution_identity_and_existing_producer_recommendation(self):
        with patch("subprocess.run", side_effect=AssertionError("no native rebuild")):
            snapshot = sub.snapshot(self.project)
            cohorts = plan.cohorts([], snapshot)
        resource = next(row for row in snapshot.entities if row.kind == "resource")
        self.assertEqual(resource.storage, (907328, 907536))
        self.assertEqual(resource.execution, (0x04001000, 0x040010D0))
        self.assertFalse(any(row.kind == "data" for row in snapshot.entities))
        membership = next(row for row in snapshot.memberships if row.entity_id == resource.id)
        self.assertEqual(membership.state, "unknown")
        self.assertEqual(len(cohorts), 1)
        self.assertEqual(cohorts[0].candidate_ids, ())
        self.assertEqual(cohorts[0].estimated_new_bytes, 0)
        self.assertIn("resources/rsp/boot.s", cohorts[0].write_paths)


class CapturedCoordinatorCases(SingleWriterTests):
    """Replay real source/word outcomes in the existing coordinator test harness.

    alpha/beta are harness aliases; no invented ROM, fresh native proof or delivery credit.
    """

    def setUp(self):
        super().setUp()
        captured = payloads(B)["us"]
        providers = json.loads(
            (Path(__file__).parent / "fixtures/ragewars_retained_12670/provider-holders.json").read_text()
        )
        raw = bytes.fromhex(next(row for row in providers if row["version"] == "us")["words"])
        self.captured_sources = {
            "beta": source(B),
            "alpha": (Path(__file__).parent / "fixtures/ragewars_retained_12670/provider.c").read_text(),
        }
        self.captured_compares = {
            "beta": measure_words("us", packed(captured["target"]), packed(captured["candidate"])),
            "alpha": measure_words("us", raw, raw),
        }

    def test_actual_nonexact_source_exhausts_methods_locally_while_exact_provider_leaves(self):
        run = self.cycle(land_steps=[[]], search=lambda n, text, method: None)
        self.assertEqual(self.methods(run), ["registers", "order", "permute"])
        self.assertEqual(run.result.data["landed"], ["alpha"])
        self.assertEqual(run.result.data["carryovers"], ["beta"])
        self.assertNotIn("beta", run.published)
        self.assertEqual((self.root / "work/beta/beta.c").read_text(), self.captured_sources["beta"])
        self.assertEqual(run.names("fn.fuzzy_landed"), [])
        self.assertEqual(run.names("fn.committed", "beta"), [])
        queued = run.names("fn.queued", "beta")[0]
        creative = run.names("fn.creative", "beta")[0]
        self.assertEqual(queued["subsystem_ref"], creative["subsystem_ref"])
        self.assertEqual(creative["best_percent"], self.captured_compares["beta"].percent)
        file = self.root / "work/beta/beta.c"
        context = sub.aggregate(sub.Facts(missing_inputs=("actual replay unknown semantics",)))
        current = ladder.Ladder(best=self.captured_compares["beta"].percent, hints=hints.for_subject("beta", context))
        with patch.object(ladder, "target_assembly", return_value="captured target retained elsewhere"):
            trouble = ladder.write_trouble(object(), object(), "beta", file, current, "captured word mismatch")
        self.assertIn(context.evidence_key, trouble.read_text())
        self.assertIn("unknown context", trouble.read_text())

    def test_actual_unavailable_comparison_keeps_best_captured_source_local(self):
        run = self.cycle(land_steps=[[]], compare_fault=True)
        self.assertNotIn("beta", run.published)
        self.assertEqual((self.root / "work/beta/beta.c").read_text(), self.captured_sources["beta"])
        self.assertEqual(run.names("fn.fuzzy_landed"), [])
        self.assertIsNone(run.names("fn.compare.done", "beta")[0]["best_percent"])
