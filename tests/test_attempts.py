"""One strict Ledger: immutable union, incremental indexing, imported bounds and source-bound receipts."""

import json
from unittest.mock import patch

from tests.ledger_fixture import log_attempt
from tests.project_fixture import ProjectCase
from unbake import migrate_state
from unbake.config import Held
from unbake.inputs import DependencySet
from unbake.work import attempts


class LedgerTests(ProjectCase):
    def attempt(self, percent=50.0):
        return attempts.Attempt(
            attempts.now(), "beta", "0" * 64, 12, {"us": {"percent": percent}}, percent, False, 30.0, "ido-7.1"
        )

    def test_independent_branch_attempts_union_and_duplicate_id_counts_once(self):
        base = b""
        log_attempt(self.project, self.attempt(40))
        target = self.project.root / attempts.PATH
        ours = target.read_bytes()
        target.unlink()
        log_attempt(self.project, self.attempt(70))
        theirs = target.read_bytes()
        merged = attempts.Ledger.merge(base, ours, theirs)
        target.write_bytes(merged)
        summary = attempts.Ledger(self.project).summaries()["beta"]
        self.assertEqual((summary.attempts, summary.minutes, summary.best["us"]), (2, 1.0, 70))
        self.assertEqual(attempts.Ledger.merge(base, merged, ours), merged)
        self.assertEqual(len(merged.splitlines()), 2)

    def test_conflicting_equal_id_and_missing_parent_refuse(self):
        log_attempt(self.project, self.attempt())
        data = (self.project.root / attempts.PATH).read_bytes()
        row = json.loads(data)
        row["result"]["value"]["attempt"]["bytes"] = 99
        with self.assertRaisesRegex(ValueError, "event_conflict"):
            attempts.Ledger.merge(b"", data, attempts.encoded(row))
        row = json.loads(data)
        row["parents"] = ["1" * 32]
        with self.assertRaisesRegex(ValueError, "ledger.parents"):
            attempts.Ledger.merge(b"", attempts.encoded(row), b"")

    def test_batch_reads_prefix_once_and_updates_once_per_append(self):
        log_attempt(self.project, self.attempt())
        with attempts.command_ledger(self.project) as history:
            for index in range(3):
                history.compare(self.attempt(50 + index), DependencySet((), {}, {}))
                self.assertEqual(history.summaries()["beta"].attempts, index + 2)
            self.assertEqual((history.prefix_reads, history.incremental_updates), (1, 4))
            target = self.project.root / attempts.PATH
            temporary = target.with_suffix(".replacement")
            temporary.write_bytes(target.read_bytes())
            temporary.replace(target)
            history.summaries()
            self.assertEqual(history.prefix_reads, 2)

    def test_unknown_schema_duplicate_json_bool_counts_and_truncation_refuse(self):
        log_attempt(self.project, self.attempt())
        target = self.project.root / attempts.PATH
        valid = target.read_bytes()
        row = json.loads(valid)
        cases = []
        for field, value in (("schema", True), ("schema", 1), ("work", {"native_calls": True})):
            cases.append(attempts.encoded({**row, field: value}) + b"\n")
        cases.extend((valid[:-1], valid.replace(b'"schema":2', b'"schema":2,"schema":2')))
        for content in cases:
            target.write_bytes(content)
            with self.assertRaises(Held):
                attempts.Ledger(self.project).summaries()

    def test_rename_adds_edge_and_never_rewrites_historical_records(self):
        log_attempt(self.project, self.attempt())
        target = self.project.root / attempts.PATH
        before = target.read_bytes()
        history = attempts.Ledger(self.project)
        history.rename({"beta": "beta_new"})
        self.assertTrue(target.read_bytes().startswith(before))
        self.assertEqual(history.summaries()["beta_new"].attempts, 1)
        self.assertNotIn("beta", history.summaries())

    def test_offline_migration_keeps_overlap_as_bounded_import_without_proof(self):
        legacy = self.project.root / "attempts.json"
        summary = {"bytes": 12, "best": {"us": 60.0}, "exact": False, "minutes": 2.0, "attempts": 3}
        legacy.write_text(json.dumps({"v": 1, "functions": {"beta": summary}}))
        directory = self.project.work / "beta"
        directory.mkdir(parents=True)
        local = directory / "attempts.jsonl"
        local.write_bytes(attempts.encoded(self.attempt(70).document()) + b"\n")
        planned = migrate_state.plan(self.project)
        self.assertEqual((planned["counts"]["files"], planned["counts"]["local_logs"]), (2, 1))
        with patch("unbake.report.state.inventory"):
            result = migrate_state.apply(self.project, planned)
        history = attempts.Ledger(self.project)
        imported = history.summaries()["beta"]
        self.assertEqual((imported.attempts, imported.imported_bounds, imported.best["us"]), (3, (3, 4), 70))
        self.assertEqual(len(history.order), 1)
        self.assertFalse(legacy.exists())
        self.assertFalse(local.exists())
        self.assertTrue((self.project.root / result["backup"] / "attempts.json").is_file())
        self.assertEqual(
            migrate_state.apply(self.project, migrate_state.plan(self.project)), {"reused": True, "events": 0}
        )

    def test_retired_state_has_named_offline_action(self):
        (self.project.root / "attempts.json").write_text("{}")
        with self.assertRaises(Held) as caught:
            attempts.Ledger(self.project).summaries()
        self.assertEqual(caught.exception.key, "ledger.migration")
        self.assertEqual(caught.exception.fault.cause.action.argv, ("migrate-state", "--plan"))
