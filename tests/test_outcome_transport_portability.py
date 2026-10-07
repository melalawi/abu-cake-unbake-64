"""Actual failure vectors and path-bearing events survive the one public owner."""

import ast
import gzip
import hashlib
import json
from pathlib import Path
from unittest.mock import patch

from tests.project_fixture import ProjectCase
from unbake import land, migrate_state, process
from unbake.config import Held
from unbake.project.headers import Graph, Search, TreeView
from unbake.typemap import solver
from unbake.work import attempts

FIXTURE = Path(__file__).parent / "fixtures/outcome_transport"
FAILURES = json.loads((FIXTURE / "five_holder_failures.json").read_text())
EVENTS = json.loads(gzip.decompress((FIXTURE / "three_path_events.json.gz").read_bytes()))
_search = ast.parse(EVENTS[0]["dependencies"]["values"]["search"], mode="eval").body
assert isinstance(_search, ast.Call)
_roots = next(field.value for field in _search.keywords if field.arg == "include_roots")
assert isinstance(_roots, ast.Tuple) and isinstance(_roots.elts[0], ast.Call)
_root = _roots.elts[0].args[0]
assert isinstance(_root, ast.Constant) and isinstance(_root.value, str)
OLD_ROOT = str(Path(_root.value).parent)


class OutcomeTransportTests(ProjectCase):
    def test_public_aggregate_keeps_all_five_actual_rows_and_starts_no_work(self):
        cause = process.named("land.fuzzy_compile", "five holding versions refused", owner="land", stage="land")
        error = Held(cause, failures=tuple(FAILURES))
        with (
            patch.object(land, "land", side_effect=error),
            patch.object(process, "run_native") as native,
            patch.object(solver, "solve") as solve,
            patch.object(land.compare, "compare") as compare,
        ):
            result = land.publish(self.project, self.host, [self.project.work / "alpha.c"], fuzzy=True)
        row = result.document()["failed"]["alpha"]
        self.assertEqual((native.call_count, solve.call_count, compare.call_count), (0, 0, 0))
        self.assertEqual(len(row.get("failures", ())), 5)
        self.assertEqual(row["failures"], FAILURES)
        self.assertEqual(row["fault"]["cause"]["key"], "land.fuzzy_compile")
        frames = [f for f in row["fault"]["chain"] if f.get("evidence", {}).get("failures")]
        self.assertEqual(frames[0]["evidence"]["failures"], FAILURES)
        self.assertEqual(result.commits, [])

    def test_real_aggregate_producer_scopes_function_and_proposed_inputs_without_native_work(self):
        from unbake import pool
        from unbake.layout import split

        identities = []
        with (
            patch.object(pool, "run", return_value=[(row, set()) for row in FAILURES]) as workers,
            patch.object(split, "holding_versions", return_value=[row["version"] for row in FAILURES]),
            patch.object(process, "run_native") as native,
            patch.object(solver, "solve") as solve,
        ):
            for index, function in enumerate(
                (
                    "func_802181FC_de",
                    "func_8020BE7C_de",
                    "func_802016FC_de",
                    "func_80201ACC_de",
                    "func_80217290_de",
                    "func_802181FC_de",
                )
            ):
                source = "int " + function + "(void) { return 1; }\n"
                with self.assertRaises(Held) as caught:
                    land.prove(
                        self.project,
                        self.host,
                        function,
                        source,
                        {},
                        self.project.work / ("identity-" + str(index)),
                        fuzzy=True,
                    )
                fault = caught.exception.fault
                self.assertEqual(fault.cause.subject, function)
                self.assertEqual(
                    fault.cause.dependency_set.values["proposed_source_sha256"],
                    hashlib.sha256(source.encode()).hexdigest(),
                )
                self.assertEqual(len(caught.exception.failures), 5)
                identities.append((fault.cause.id, fault.cause.blocked_key))
        self.assertEqual(len(set(identities[:5])), 5)
        self.assertEqual(identities[0], identities[-1])
        self.assertEqual((workers.call_count, native.call_count, solve.call_count), (6, 0, 0))

    def test_typed_native_location_stderr_and_inner_owner_survive_aggregate_capture(self):
        failures = []
        for row in FAILURES:
            version = row["version"]
            fault = process.Fault(
                process.named(
                    "map.schema",
                    row["reason"],
                    owner="typemap.mapping",
                    stage="map",
                    subject=version,
                    location=process.SourceLocation("src/alpha.c", 19, 2),
                )
            )
            failures.append({**row, "fault": fault.document()})
        aggregate = Held(
            process.named("land.fuzzy_compile", "five refused", owner="land", stage="land"), failures=tuple(failures)
        )
        wrapped = process.capture(
            aggregate, cause=process.named("cli.publish", "publication refused", owner="cli.publish", stage="publish")
        )
        payload = json.loads(attempts.encoded(wrapped.document()))
        values = payload["chain"][0]["evidence"]["failures"]
        self.assertEqual(len(values), 5)
        self.assertEqual(
            [
                (f["fault"]["cause"]["owner"], f["fault"]["cause"]["stage"], f["fault"]["cause"]["location"])
                for f in values
            ],
            [("typemap.mapping", "map", {"path": "src/alpha.c", "line": 19, "column": 2})] * 5,
        )


class PortableHistoryTests(ProjectCase):
    def rows(self):
        rows = json.loads(json.dumps(EVENTS).replace(OLD_ROOT, str(self.project.root)))
        for row in rows:
            row["project_id"] = self.project.id
            row["parents"] = []  # The selected three-event slice has no ancestors in this tiny fixture.
        return rows

    def raw_history(self):
        target = self.project.root / attempts.PATH
        rows = self.rows()
        target.write_bytes(b"".join(attempts.encoded(row) + b"\n" for row in rows))
        return target, rows

    def test_new_appends_of_real_three_events_have_no_machine_prefix_and_same_work_scores(self):
        history = attempts.Ledger(self.project)
        rows = self.rows()
        with patch.object(process, "run_native") as native, patch.object(solver, "solve") as solve:
            for row in rows:
                history.append(attempts.Event(**{**row, "parents": tuple(row["parents"])}))
        text = history.path.read_text()
        self.assertFalse("devstorage" in text)
        self.assertFalse(str(self.project.root) in text)
        self.assertNotIn("Search(", text)
        written = [json.loads(line) for line in text.splitlines()]
        self.assertEqual((len(written), native.call_count, solve.call_count, history.prefix_reads), (3, 0, 0, 0))
        self.assertEqual([r["work"] for r in written], [r["work"] for r in rows])
        self.assertEqual(
            [r["result"]["value"]["attempt"] for r in written],
            [attempts.portable_tree(self.project, r["result"]["value"]["attempt"]) for r in rows],
        )
        self.assertEqual([r["event_id"] for r in written], [r["event_id"] for r in rows])

    def test_public_representation_cutover_keeps_original_bytes_counts_and_native_errors(self):
        target, rows = self.raw_history()
        original = target.read_bytes()
        summary = attempts.Ledger(self.project).summaries()
        native_counts = [r["work"] for r in rows]
        with patch.object(process, "run_native") as native, patch.object(solver, "solve") as solve:
            planned = migrate_state.plan(self.project)
            result = migrate_state.apply(self.project, planned)
            second = migrate_state.apply(self.project, migrate_state.plan(self.project))
        self.assertEqual((result["events"], second["events"], native.call_count, solve.call_count), (3, 0, 0, 0))
        backup = self.project.root / result["backup"] / attempts.PATH
        self.assertEqual(backup.read_bytes(), original)
        self.assertEqual(hashlib.sha256(backup.read_bytes()).hexdigest(), planned["ledger"])
        self.assertFalse("devstorage" in target.read_text())
        self.assertNotIn("Search(", target.read_text())
        written = [json.loads(line) for line in target.read_bytes().splitlines()]
        self.assertEqual([r["work"] for r in written], native_counts)
        self.assertEqual(attempts.Ledger(self.project).summaries(), summary)
        self.assertEqual([r["operation_id"] for r in written], [r["operation_id"] for r in rows])
        self.assertEqual([r["result"]["proof_ids"] for r in written], [r["result"]["proof_ids"] for r in rows])
        self.assertEqual(
            [r["request"]["representation_origin"]["event_id"] for r in written], [r["event_id"] for r in rows]
        )

    def test_published_nested_fault_overlay_pins_and_watches_have_no_machine_components(self):
        rows = json.loads(gzip.decompress((FIXTURE / "published_path_events.json.gz").read_bytes()))
        for row in rows:
            row["project_id"] = self.project.id
            row["parents"] = []
        target = self.project.root / attempts.PATH
        original = b"".join(attempts.encoded(row) + b"\n" for row in rows)
        target.write_bytes(original)
        summary = attempts.Ledger(self.project).summaries()
        with patch.object(process, "run_native") as native, patch.object(solver, "solve") as solve:
            result = migrate_state.apply(self.project, migrate_state.plan(self.project))
        self.assertEqual((result["events"], native.call_count, solve.call_count), (11, 0, 0))
        text = target.read_text()
        for marker in ("devstorage", "abu-admin", "own-contract-compare-", "Search("):
            self.assertFalse(marker in text, marker)
        self.assertEqual((self.project.root / result["backup"] / attempts.PATH).read_bytes(), original)
        self.assertEqual(attempts.Ledger(self.project).summaries(), summary)
        current = [json.loads(line) for line in text.splitlines()]
        self.assertEqual([row["work"] for row in current], [row["work"] for row in rows])
        for before, after in zip(rows, current, strict=True):
            self.assertEqual(before["result"]["state"], after["result"]["state"])
            self.assertEqual(before["result"]["proof_ids"], after["result"]["proof_ids"])
            fault = after["result"]["fault"]
            if fault is not None:
                self.assertEqual(fault["cause"]["key"], before["result"]["fault"]["cause"]["key"])
                self.assertEqual(fault["cause"]["owner"], before["result"]["fault"]["cause"]["owner"])

    def test_normalized_and_original_branch_union_counts_each_real_attempt_once(self):
        target, _ = self.raw_history()
        old = target.read_bytes()
        migrate_state.apply(self.project, migrate_state.plan(self.project))
        normalized = target.read_bytes()
        merged = attempts.Ledger.merge(old, normalized, old)
        target.write_bytes(merged)
        history = attempts.Ledger(self.project)
        self.assertEqual(sum(s.attempts for s in history.summaries().values()), 3)
        self.assertEqual(len(history.order), 3)
        self.assertFalse("Search(" in merged.decode())
        self.assertEqual(attempts.Ledger.merge(old, merged, normalized), merged)

    def test_search_contract_relocation_stable_and_include_order_sensitive(self):
        one, two = self.root / "one", self.root / "two"
        records = []
        for root in (one, two):
            root.mkdir()
            view = TreeView("project", {}, (root,), root=root)
            search = Search(include_roots=(root / "first", root / "second"), macros=("-DVERSION_US",))
            graph = Graph(view, search)
            records.append(graph.closure(()).dependency_set)
        self.assertEqual(records[0].digest, records[1].digest)
        changed = (
            Graph(
                TreeView("project", {}, (one,), root=one),
                Search(include_roots=(one / "second", one / "first"), macros=("-DVERSION_US",)),
            )
            .closure(())
            .dependency_set
        )
        self.assertNotEqual(records[0].digest, changed.digest)

    def test_unknown_search_syntax_is_never_evaluated_or_replaced_with_defaults(self):
        target, rows = self.raw_history()
        rows[0]["dependencies"]["values"]["search"] = "Search(include_roots=__import__('os').system('false'))"
        target.write_bytes(b"".join(attempts.encoded(row) + b"\n" for row in rows))
        original = target.read_bytes()
        with self.assertRaises(Held) as caught:
            migrate_state.plan(self.project)
        self.assertEqual(caught.exception.key, "migration.search")
        self.assertEqual(target.read_bytes(), original)
