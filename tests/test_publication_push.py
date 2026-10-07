"""Small published units prove affected work, never a whole-project rebuild."""

import argparse
import io
from collections import Counter
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

from tests.project_fixture import ProjectCase
from unbake import config, land
from unbake.cli import publish
from unbake.cli.args import Context
from unbake.cli.main import make_parser
from unbake.config import Held
from unbake.layout import split


class PublicationPushTests(ProjectCase):
    def setUp(self):
        super().setUp()
        self.header = self.project.include[-1] / "owner.h"
        self.header.write_text("struct QueryBox { short values[9]; };\n")
        for name in ("alpha", "beta", "gamma"):
            prefix = '#include "owner.h"\n' if name != "gamma" else ""
            (self.project.src / f"{name}.c").write_text(prefix + f"int {name}(void) {{return 1;}}\n")
        for version in self.project.versions:
            path = self.project.version(version).split
            path.write_text(path.read_text().replace("asm,", "c,"))
        self.project = config.load(self.project.root)
        self.rows = {version: split.functions(self.project, version) for version in self.project.versions}
        self.compiler_pin = self.project.compiler_for("alpha").sha256
        self.compiler_pin.write_text("immutable compiler pin\n")

    def boundaries(self):
        from unbake.project import publication_push as flow

        return (
            patch.object(flow.split, "functions", side_effect=lambda p, v: self.rows[v]),
            patch.object(flow.drivers, "flags", side_effect=lambda p, v, u, **kwargs: ["-O2", "-D" + v]),
            patch.object(flow.attempts, "fuzzy_sources", return_value=frozenset()),
        )

    def test_snapshot_reads_each_actual_input_once_with_two_version_scans_and_no_native_work(self):
        from pathlib import Path

        from unbake.project import publication_push as flow

        reads = Counter()
        actual_read = Path.read_bytes

        def read(path):
            reads[path] += 1
            return actual_read(path)

        boundaries = self.boundaries()
        with (
            boundaries[0] as scans,
            boundaries[1],
            boundaries[2],
            patch.object(Path, "read_bytes", read),
            patch.object(flow.runner, "dependencies") as native,
            patch.object(flow.pool, "run") as workers,
        ):
            scopes = flow.snapshot(self.project, self.host)
        expected = {
            self.header,
            self.compiler_pin,
            *(self.project.src / f"{n}.c" for n in ("alpha", "beta", "gamma")),
            *(self.project.version(v).symbols for v in self.project.versions),
        }
        self.assertEqual(reads, dict.fromkeys(expected, 1))
        self.assertEqual(scans.call_count, 2)
        self.assertEqual(len(scopes), 6)
        self.assertTrue(all(len(pin) == 64 for pin in scopes.values()))
        native.assert_not_called()
        workers.assert_not_called()

    def test_header_change_reproves_four_scopes_and_keeps_two_unrelated_scopes(self):
        from unbake.project import publication_push as flow

        boundaries = self.boundaries()
        with boundaries[0], boundaries[1], boundaries[2]:
            before = flow.snapshot(self.project, self.host)
            self.header.write_text(self.header.read_text() + "extern int added;\n")
            with (
                patch.object(
                    flow.pool, "run", side_effect=lambda h, action, jobs: [action(job) for job in jobs]
                ) as workers,
                patch.object(land, "_builds_row", return_value=(True, set())) as native,
            ):
                scopes, after = flow.reconcile(self.project, self.host, before)
        self.assertEqual(native.call_count, 4)
        self.assertEqual(workers.call_count, 1)
        self.assertEqual(
            {(r["function"], r["version"]) for r in scopes},
            {("alpha", "us"), ("alpha", "eu"), ("beta", "us"), ("beta", "eu")},
        )
        self.assertEqual(before["gamma", "us"], after["gamma", "us"])
        self.assertEqual(before["gamma", "eu"], after["gamma", "eu"])

    def test_rebased_source_admission_scans_affected_units_once_before_workers(self):
        from unbake.project import publication_push as flow

        boundaries = self.boundaries()
        with boundaries[0], boundaries[1], boundaries[2]:
            before = flow.snapshot(self.project, self.host)
            self.header.write_text(self.header.read_text() + "extern int added;\n")
            finding = SimpleNamespace(path="src/alpha.c", finding=SimpleNamespace(fakematch=None, line=2))
            with (
                patch("unbake.decomp.checks.findings", return_value=SimpleNamespace(rows=[finding])) as scans,
                patch("unbake.decomp.checks.plain", return_value="source rule blocked"),
                patch.object(flow.pool, "run") as workers,
                self.assertRaisesRegex(Held, "publish.push_rules"),
            ):
                flow.reconcile(self.project, self.host, before)
        self.assertEqual(scans.call_count, 1)
        self.assertEqual(scans.call_args.args[1], (self.project.src / "alpha.c", self.project.src / "beta.c"))
        workers.assert_not_called()

    def test_one_versions_link_inputs_reprove_only_that_versions_three_scopes(self):
        from unbake.project import publication_push as flow

        boundaries = self.boundaries()
        with boundaries[0], boundaries[1], boundaries[2]:
            before = flow.snapshot(self.project, self.host)
            symbols = self.project.version("eu").symbols
            symbols.write_text(symbols.read_text() + "another = 0x80001234;\n")
            with (
                patch.object(flow.pool, "run", side_effect=lambda h, action, jobs: [action(job) for job in jobs]),
                patch.object(land, "_builds_row", return_value=(True, set())) as native,
            ):
                scopes, _ = flow.reconcile(self.project, self.host, before)
        self.assertEqual(native.call_count, 3)
        self.assertEqual({r["version"] for r in scopes}, {"eu"})

    def test_unit_flags_change_only_the_two_proofs_for_that_unit(self):
        from unbake.project import publication_push as flow

        before_project = self.project
        after_project = replace(self.project, unit_flags={"alpha": ("-O1",)})
        boundaries = self.boundaries()
        with boundaries[0], boundaries[2]:
            before = flow.snapshot(before_project, self.host)
            with patch.object(flow.pool, "run", side_effect=lambda h, action, jobs: [None for job in jobs]) as workers:
                scopes, _ = flow.reconcile(after_project, self.host, before)
        self.assertEqual([(r["function"], r["version"]) for r in scopes], [("alpha", "us"), ("alpha", "eu")])
        self.assertEqual(len(workers.call_args.args[2]), 2)

    def git_boundary(self, *, conflict=False, fail_push=False, docs_only=False):
        calls = []
        original = self.header.read_text()

        def git(project, *args):
            calls.append(args)
            if args == ("rev-parse", "HEAD"):
                return "rebased" if ("rebase", "FETCH_HEAD") in calls else "local"
            if args == ("rev-parse", "FETCH_HEAD"):
                return "remote"
            if args[0] == "merge-base":
                return "base"
            if args[0] == "rebase" and args[1] == "FETCH_HEAD":
                if conflict:
                    raise Held("git", "git.rebase: conflict")
                if not docs_only:
                    self.header.write_text(original + "extern int added;\n")
            if args[0] == "reset":
                self.header.write_text(original)
            if args[0] == "push" and fail_push:
                raise Held("git", "git.push: transport refused")
            return ""

        return git, calls

    def run_push(self, *, native=(True, set()), **options):
        from unbake.project import publication_push as flow

        git, calls = self.git_boundary(**options)
        boundaries = self.boundaries()
        with (
            boundaries[0],
            boundaries[1],
            boundaries[2],
            patch.object(flow, "_git", side_effect=git),
            patch.object(flow, "snapshot", wraps=flow.snapshot) as snapshots,
            patch.object(
                flow.pool, "run", side_effect=lambda h, action, jobs: [action(job) for job in jobs]
            ) as workers,
            patch.object(
                land,
                "_builds_row",
                **({"side_effect": native} if isinstance(native, BaseException) else {"return_value": native}),
            ) as proof,
        ):
            try:
                result = flow.push(self.project, self.host, "origin")
            except BaseException as error:
                result = error
        return result, calls, snapshots.call_count, workers.call_count, proof.call_count

    def test_concurrent_rebase_reconciles_before_one_push_with_two_snapshots(self):
        result, calls, snapshots, workers, native = self.run_push()
        self.assertEqual((snapshots, workers, native), (2, 1, 4))
        self.assertEqual(len(calls), 10)
        self.assertEqual(calls.count(("rebase", "FETCH_HEAD")), 1)
        self.assertEqual(calls.count(("push", "--", "origin", "HEAD:refs/heads/main")), 1)
        self.assertEqual(result["head"], "rebased")
        self.assertEqual(len(result["reconciled"]), 4)
        self.assertTrue(all("force" not in word for call in calls for word in call))

    def test_document_only_rebase_starts_zero_workers_and_proves_zero_units(self):
        result, calls, snapshots, workers, native = self.run_push(docs_only=True)
        self.assertEqual((snapshots, workers, native), (2, 0, 0))
        self.assertEqual(result["reconciled"], [])
        self.assertEqual(sum(call[0] == "push" for call in calls), 1)

    def test_proof_refusal_restores_before_view_and_never_pushes(self):
        original = self.header.read_bytes()
        result, calls, snapshots, workers, native = self.run_push(native=(False, set()))
        self.assertIsInstance(result, Held)
        self.assertEqual(result.key, "publish.push_proof")
        self.assertEqual(len(result.failures), 4)
        self.assertEqual((snapshots, workers, native), (2, 1, 4))
        self.assertEqual(sum(call[0] == "push" for call in calls), 0)
        self.assertEqual(calls[-1], ("reset", "--keep", "local"))
        self.assertEqual(self.header.read_bytes(), original)

    def test_interrupted_proof_remains_eligible_for_a_fresh_rebase_and_proof(self):
        result, calls, snapshots, workers, native = self.run_push(native=KeyboardInterrupt())
        self.assertIsInstance(result, KeyboardInterrupt)
        self.assertEqual((snapshots, workers, native), (2, 1, 1))
        self.assertEqual(calls[-1], ("reset", "--keep", "local"))
        self.assertEqual(sum(call[0] == "push" for call in calls), 0)
        result, calls, snapshots, workers, native = self.run_push()
        self.assertEqual((snapshots, workers, native), (2, 1, 4))
        self.assertEqual(len(result["reconciled"]), 4)

    def test_rebase_conflict_aborts_without_any_native_work_or_push(self):
        result, calls, snapshots, workers, native = self.run_push(conflict=True)
        self.assertEqual(result.key, "publish.push_conflict")
        self.assertEqual((snapshots, workers, native), (1, 0, 0))
        self.assertEqual(calls[-1], ("rebase", "--abort"))
        self.assertEqual(sum(call[0] == "push" for call in calls), 0)

    def test_unchanged_remote_transport_refusal_does_not_loop_or_rebase_again(self):
        result, calls, snapshots, workers, native = self.run_push(fail_push=True)
        self.assertEqual(result.key, "publish.push_transport")
        self.assertEqual(sum(call[0] == "push" for call in calls), 1)
        self.assertEqual(sum(call[0] == "fetch" for call in calls), 2)
        self.assertEqual((snapshots, workers, native), (2, 1, 4))


class PublicPushCliTests(ProjectCase):
    def test_public_options_cover_current_measurement_and_push_without_private_driver(self):
        args = make_parser().parse_args(["publish", "--compare", "--events", "--push", "origin", "alpha.c"])
        self.assertEqual((args.compare, args.events, args.push), (True, True, "origin"))

    def test_compare_first_runs_once_per_source_before_each_public_native_gate(self):
        files = [self.root / f"{name}.c" for name in ("alpha", "beta")]
        calls = []

        def measure(project, host, file, **kwargs):
            calls.append(("compare", file.stem))

        def native(project, host, file, **kwargs):
            calls.append(("land", file.stem))
            return "accepted-" + file.stem

        with (
            patch.object(land.compare, "compare", side_effect=measure) as compares,
            patch.object(land, "land", side_effect=native) as proofs,
            patch.object(land.steps, "ensure", return_value=[]),
        ):
            result = land.publish(self.project, self.host, files, compare_first=True)
        self.assertEqual(calls, [("compare", "alpha"), ("land", "alpha"), ("compare", "beta"), ("land", "beta")])
        self.assertEqual((compares.call_count, proofs.call_count), (2, 2))
        self.assertEqual(result.landed, ["alpha", "beta"])

    def test_public_push_retry_needs_no_new_publication(self):
        from unbake.project import publication_push as flow

        args = argparse.Namespace(files=[], original=[], require_version=None, events=False, fuzzy=False, push="origin")
        context = Context("publish", args, self.project.root, None, io.StringIO(), self.host)
        with patch.object(land, "land") as native, patch.object(flow, "push", return_value={"head": "pushed"}) as push:
            result = publish.run(context)
        native.assert_not_called()
        self.assertEqual(push.call_count, 1)
        self.assertEqual(result.data["push"]["head"], "pushed")
