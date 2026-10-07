"""Public publish reaches reconciliation before fold's strict read on saved RW source and catalog slices.

Native compile/proof, declaration solving and Git are seams; CLI dispatch, admission, map inference,
fold/view, source rules, writer and receipts remain real. No ROM build or full type solve runs.
"""

import argparse
import hashlib
import io
import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import toml

from tests.project_fixture import ProjectCase
from unbake import buildfiles, config, land, steps
from unbake.cli import publish
from unbake.cli.args import Context
from unbake.config import Held
from unbake.fold import apply as fold_apply
from unbake.fold import declarations
from unbake.layout import map as layout_map
from unbake.layout import modules, split
from unbake.project import generated_state
from unbake.report import progress
from unbake.work import attempts

FIXTURE = Path(__file__).parent / "fixtures/rw_membership/public.json"
MISSING = "func_802C1B60_eu_x"
FUZZY = "func_8029B278_de"
EXACT = "func_80200880_de"


class PublicMembershipTests(ProjectCase):
    versions = ("de", "eu", "eu-x", "us", "us-rev1")

    def setUp(self):
        super().setUp()
        self.payload = json.loads(FIXTURE.read_text())
        configured = toml.loads((self.project.root / "config.toml").read_text())
        configured["project"]["names_from"] = self.payload["names_from"]
        configured["project"]["default_compiler"] = "gcc-2.8.1-sn64"
        configured["compilers"] = self.payload["compiler_configs"]
        (self.project.root / "config.toml").write_text(toml.dumps(configured))
        for version, data in self.payload["versions"].items():
            self.project.version(version).split.write_text(data["yaml"])
            self.project.version(version).symbols.write_text(data["symbols"])
        self.layout = self.project.root / "layout.toml"
        self.layout.write_text(toml.dumps(self.payload["layout"]))
        self.project = config.load(self.project.root)
        self.files = {}
        for function, data in self.payload["drafts"].items():
            path = self.project.work / function / (function + ".c")
            path.parent.mkdir(parents=True)
            path.write_text(data["source"])
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), data["sha256"])
            self.files[function] = path
            attempts.append(
                self.project,
                attempts.Attempt(
                    "2026-10-07T04:29:20+00:00",
                    function,
                    data["sha256"],
                    328 if function == FUZZY else 220,
                    data["versions"],
                    data["best_percent"],
                    function == EXACT,
                    0,
                    data["compiler"],
                ),
            )
        self.recorded = {}
        for version, data in self.payload["versions"].items():
            functions = [
                modules.Function(Path(f.path).name, "span_1000", f.start, f.end, f.address)
                for f in split.functions(self.project, version)
            ]
            self.recorded[version] = (
                functions,
                lambda f, data=data: [
                    int(data["words"][f.name][i : i + 8], 16) for i in range(0, len(data["words"][f.name]), 8)
                ],
                lambda address: False,
                None,
            )
        self.processes = self.enterContext(
            patch.object(subprocess, "Popen", side_effect=AssertionError("native process"))
        )
        self.folds = []

    def invoke(self, functions, *, fuzzy):
        stream = io.StringIO()
        args = argparse.Namespace(
            files=[self.files[n] for n in functions], original=[], require_version=None, events=True, fuzzy=fuzzy
        )
        context = Context("publish", args, self.project.root, None, stream, self.host)

        def native_proof(project, host, function, source, headers, stage, **kwargs):
            self.assertEqual([n for n, _ in self.folds], list(functions[: len(self.folds)]))
            self.assertIn(MISSING, layout_map.load(project).owners)
            data = self.payload["drafts"][function]
            scores = {v: {**row, "compiled": True} for v, row in data["versions"].items()}
            return land.Proof(list(self.versions), set(), scores if kwargs.get("fuzzy") else None)

        def declared(project, host, function, source, versions, **kwargs):
            # Real fold/view has already loaded the map; only the full type solver is replaced.
            self.folds.append((function, layout_map.load(project).owners))
            return [split.Edit(project.src / (function + ".c"), "", source, versions)]

        with (
            patch.object(layout_map, "ensure", wraps=layout_map.ensure) as admit,
            patch.object(modules, "infer", wraps=modules.infer) as infer,
            patch.object(modules, "facts", side_effect=lambda p, v: self.recorded[v]) as facts,
            patch.object(layout_map.atomic_files, "write", wraps=layout_map.atomic_files.write) as writes,
            patch.object(fold_apply, "view", wraps=fold_apply.view) as view,
            patch.object(declarations, "folded_edits", side_effect=declared) as solve,
            patch.object(land, "prove", side_effect=native_proof) as proof,
            patch.object(land, "_commit") as commit,
            patch.object(
                land,
                "_git",
                side_effect=lambda p, *a: f"accepted-{commit.call_count}\n" if a == ("rev-parse", "HEAD") else "",
            ),
            patch.object(buildfiles, "write", return_value=[]) as buildfiles_write,
            patch.object(progress, "write", return_value=[]),
            patch.object(steps, "record"),
            patch.object(steps, "ensure", return_value=[]) as freshness,
        ):
            result = publish.run(context)
        counts = (
            admit.call_count,
            infer.call_count,
            facts.call_count,
            sum(call.args[0] == self.layout for call in writes.call_args_list),
            view.call_count,
            solve.call_count,
            proof.call_count,
            commit.call_count,
            buildfiles_write.call_count,
            freshness.call_count,
        )
        self.processes.assert_not_called()
        return result, [json.loads(line) for line in stream.getvalue().splitlines()], counts

    def assert_members(self):
        members = layout_map.catalog(self.project)
        mapped = layout_map.load(self.project)
        self.assertEqual(tuple(sorted(mapped.owners)), tuple(self.payload["expected_members"]))
        self.assertEqual((len(mapped.groups), len(mapped.owners)), (4, 10))
        self.assertEqual((members[MISSING].address, members[MISSING].versions), (0x802C1B60, ("eu-x",)))
        original_members = {n: m for n, m in members.items() if n != MISSING}
        original = layout_map.validate(self.payload["layout"], self.versions, original_members)
        for name, group in original.owners.items():
            self.assertEqual(mapped.owners[name], group)

    def test_public_fuzzy_batch_admits_once_before_both_real_fold_views(self):
        before = {v: self.project.version(v).split.read_bytes() for v in self.versions}
        result, events, counts = self.invoke((FUZZY, EXACT), fuzzy=True)
        # Work first: current main never reaches admission or native proof for either source.
        self.assertEqual(counts, (2, 1, 5, 1, 2, 2, 2, 2, 2, 0), result.data)
        self.assertEqual((result.status, result.data["failed"], result.data["landed"]), ("ok", {}, [FUZZY, EXACT]))
        self.assertEqual(
            [(e["event"], e["function"], e["commit"]) for e in events],
            [("fn.committed", FUZZY, "accepted-1"), ("fn.committed", EXACT, "accepted-2")],
        )
        self.assertEqual(result.data["fuzzy"][FUZZY]["score"], self.payload["drafts"][FUZZY]["best_percent"])
        self.assertEqual({v: self.project.version(v).split.read_bytes() for v in self.versions}, before)
        for function in (FUZZY, EXACT):
            source = self.project.src / (function + ".c")
            self.assertEqual(attempts.unguarded(source.read_text()), self.payload["drafts"][function]["source"])
        self.assert_members()

    def test_public_exact_route16_source_reaches_writer_after_reconciliation(self):
        result, events, counts = self.invoke((EXACT,), fuzzy=False)
        self.assertEqual(counts, (1, 1, 5, 1, 1, 1, 1, 1, 1, 1), result.data)
        self.assertEqual((result.status, result.data["failed"], result.data["landed"]), ("ok", {}, [EXACT]))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["proof"]["compared_sha256"], self.payload["drafts"][EXACT]["sha256"])
        self.assertEqual(events[0]["proof"]["versions"], list(self.versions))
        self.assertEqual((self.project.src / (EXACT + ".c")).read_text(), self.payload["drafts"][EXACT]["source"])
        self.assert_members()

    def test_authored_unknown_member_is_still_refused_without_inference_or_fold(self):
        value = self.payload["layout"]
        value["group"][0]["members"].append("func_absent")
        self.layout.write_text(toml.dumps(value))
        before = self.layout.read_bytes()
        result, events, counts = self.invoke((FUZZY,), fuzzy=True)
        self.assertEqual(counts, (1, 0, 0, 0, 0, 0, 0, 0, 0, 0))
        self.assertEqual(result.data["failed"][FUZZY]["key"], "layout.member.func_absent")
        self.assertEqual((events, self.layout.read_bytes()), ([], before))

    def test_generated_header_admission_guard_still_precedes_catalog_work(self):
        before = self.layout.read_bytes()
        with patch.object(generated_state, "reconcile", return_value=["include/edited.h"]):
            result, events, counts = self.invoke((FUZZY,), fuzzy=True)
        self.assertEqual(counts, (0, 0, 0, 0, 0, 0, 0, 0, 0, 0))
        self.assertEqual(result.data["failed"][FUZZY]["key"], "land.generated_edit")
        self.assertEqual((events, self.layout.read_bytes()), ([], before))

    def test_stale_exact_compare_refuses_before_catalog_work(self):
        self.files[EXACT].write_text(self.files[EXACT].read_text() + "\n")
        before = self.layout.read_bytes()
        result, events, counts = self.invoke((EXACT,), fuzzy=False)
        self.assertEqual(counts, (0, 0, 0, 0, 0, 0, 0, 0, 0, 0))
        self.assertEqual(result.data["failed"][EXACT]["key"], "land.not_compared")
        self.assertEqual((events, self.layout.read_bytes()), ([], before))

    def test_read_only_map_load_remains_strict_and_does_no_admission(self):
        before = self.layout.read_bytes()
        with patch.object(layout_map, "ensure", wraps=layout_map.ensure) as ensure:
            with self.assertRaisesRegex(Held, f"layout.member.{MISSING}: missing from map"):
                layout_map.load(self.project)
            ensure.assert_not_called()
        self.assertEqual(self.layout.read_bytes(), before)
        self.processes.assert_not_called()
