
"""Old SQLite generations rebuild at the solver boundary, even with a warm marker."""

import json
import sqlite3
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from tests.typemap import test_solve_reuse
from unbake import cache, steps
from unbake.config import Held
from unbake.typemap import solver, types_db


class DatabaseUpgradeTests(test_solve_reuse.SolveReuseFixture):
    solve = test_solve_reuse.FactReuseTests.solve

    def test_old_meta_without_inference_keys_rebuilds_for_warm_and_changed_inputs(self):
        for changed in (False, True):
            with self.subTest(changed=changed):
                self.solve()
                with closing(sqlite3.connect(self.database)) as connection, connection:
                    connection.execute("PRAGMA user_version = 0")
                    connection.execute("DELETE FROM meta WHERE key LIKE 'inference_%'")
                cache.forget(["types-db"])
                if changed:
                    self.source.write_text("int alpha(void) { return 9; }\n")
                calls = self.published.call_count
                revision = types_db.meta(self.database, "revision")
                self.solve()
                self.assertEqual(self.published.call_count, calls + 1)
                self.assertEqual(types_db.meta(self.database, "revision"), revision + 1)
                self.assertIsNotNone(types_db.meta(self.database, "inference_key"))
                self.assertIsNotNone(steps.recorded(self.project, "types"))

    def test_step_cache_cannot_reuse_old_meta_even_when_its_key_matches(self):
        self.solve()
        fixture = Path(__file__).parent / "fixtures/ragewars_vec3/types_legacy_meta.json"
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute("PRAGMA user_version = 0")
            connection.execute("DELETE FROM meta")
            connection.executemany("INSERT INTO meta VALUES (?, ?)", json.loads(fixture.read_text()).items())
        self.assertEqual(types_db.meta(self.database, "schema"), 1)
        self.assertIsNone(types_db.meta(self.database, "inference_key", default=None))
        calls = self.published.call_count
        step = steps.STEPS["types"]
        with patch.object(solver, "readiness", return_value=solver.Readiness("unchanged", {}, [])):
            cached_key = step.key(self.project, self.host)
        with (
            patch.object(steps, "order", return_value=["types"]),
            patch.object(
                steps,
                "STEPS",
                {"types": replace(step, key=lambda *args: cached_key, run=lambda *args: self.solve()["changes"])},
            ),
        ):
            steps.record(self.project, "types", cached_key)
            result = steps.ensure(self.project, self.host, ["types"])
        self.assertTrue(any(row.ran for row in result))
        self.assertEqual(self.published.call_count, calls + 1)
        self.assertEqual(types_db.meta(self.database, "revision"), 13)
        self.assertTrue(types_db.compatible(self.database))

    def test_stale_types_rebuild_before_damaged_headers_regenerate(self):
        self.solve()
        fixture = Path(__file__).parent / "fixtures/ragewars_vec3/types_legacy_meta.json"
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute("PRAGMA user_version = 0")
            connection.execute("DELETE FROM meta")
            connection.executemany("INSERT INTO meta VALUES (?, ?)", json.loads(fixture.read_text()).items())
        header = self.project.include[0] / "alpha.h"
        header.write_text("damaged")
        ran = []

        def rebuild(*args):
            ran.append("types")
            return self.solve()["changes"]

        def regenerate(*args):
            self.assertTrue(types_db.compatible(self.database), "headers read the stale database before the types step")
            ran.append("headers")
            header.write_text("regenerated")

        table = {
            "types": replace(steps.STEPS["types"], key=lambda *args: "warm", run=rebuild, needs=()),
            "headers": replace(
                steps.STEPS["headers"], key=lambda *args: "warm", run=regenerate, outputs=lambda project: [header]
            ),
        }
        with patch.object(steps, "STEPS", table):
            steps.record(self.project, "types", "warm")
            steps.record(self.project, "headers", "warm", {str(header.relative_to(self.project.root)): "old digest"})
            steps.ensure(self.project, self.host, ["headers"])
        self.assertEqual(ran, ["types", "headers"])

    def test_missing_database_cannot_reuse_a_matching_step_key(self):
        self.solve()
        self.database.unlink()
        step = steps.STEPS["types"]
        with (
            patch.object(steps, "order", return_value=["types"]),
            patch.object(
                steps,
                "STEPS",
                {"types": replace(step, key=lambda *args: "warm", run=lambda *args: self.solve()["changes"])},
            ),
        ):
            steps.record(self.project, "types", "warm")
            steps.ensure(self.project, self.host, ["types"])
        self.assertTrue(types_db.compatible(self.database))

    def test_step_key_invalidates_an_old_database_even_with_unchanged_inputs(self):
        self.solve()
        with patch.object(solver, "readiness", return_value=solver.Readiness("unchanged", {}, [])):
            before = steps._types_key(self.project, self.host)
            with closing(sqlite3.connect(self.database)) as connection, connection:
                connection.execute("PRAGMA user_version = 0")
                connection.execute("DELETE FROM meta WHERE key = 'inference_publication'")
            self.assertNotEqual(steps._types_key(self.project, self.host), before)

    def test_older_meta_schema_rebuilds_and_preserves_revision_and_summary(self):
        self.solve()
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute("UPDATE meta SET value = '0' WHERE key = 'schema'")
            connection.execute("INSERT INTO summary VALUES ('functions', 'alpha', 'old', '[]')")
        self.solve()
        self.assertEqual(types_db.meta(self.database, "revision"), 2)
        self.assertEqual(types_db.meta(self.database, "schema"), 1)
        self.assertEqual(self.published.call_args.args[2]["functions"]["alpha"]["semantic_sha256"], "old")

    def test_old_schema_missing_summary_and_redraft_tables_can_be_rebuilt(self):
        self.solve()
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute("PRAGMA user_version = 0")
            connection.execute("DROP TABLE summary")
            connection.execute("DROP TABLE IF EXISTS redraft")
            connection.execute("DELETE FROM meta WHERE key = 'revision'")
        self.assertEqual(__import__("unbake.work.attempts", fromlist=["ledger"]).ledger(self.project).redrafts(), {})
        self.solve()
        self.assertEqual(types_db.meta(self.database, "revision"), 1)
        self.assertTrue(types_db.compatible(self.database))

    def test_current_schema_missing_inference_key_is_named_corruption(self):
        self.solve()
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute("DELETE FROM meta WHERE key = 'inference_key'")
        calls = self.published.call_count
        with self.assertRaisesRegex(Held, "types.sqlite:.*corrupt database: missing meta inference_key"):
            self.solve()
        self.assertEqual(self.published.call_count, calls)

    def test_malformed_sqlite_and_metadata_are_named_corruption(self):
        self.database.write_bytes(b"not a SQLite database")
        with self.assertRaisesRegex(Held, "types.sqlite:.*corrupt database"):
            self.solve()
        self.database.unlink()
        self.solve()
        for key, value in (("inference_receipts", "not JSON"), ("schema", json.dumps("one"))):
            with self.subTest(key=key):
                with closing(sqlite3.connect(self.database)) as connection, connection:
                    connection.execute("UPDATE meta SET value = ? WHERE key = ?", (value, key))
                with self.assertRaisesRegex(Held, "types.sqlite:.*corrupt database"):
                    self.solve()
                with closing(sqlite3.connect(self.database)) as connection, connection:
                    connection.execute("UPDATE meta SET value = 'null' WHERE key = ?", (key,))

    def test_all_metadata_readers_name_invalid_json(self):
        self.solve()
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute("UPDATE meta SET value = 'not JSON' WHERE key = 'solution_sha256'")
        for reader in (types_db.solution, types_db.read, lambda path: types_db.meta(path, "solution_sha256")):
            with self.subTest(reader=reader), self.assertRaisesRegex(Held, "types.sqlite:.*corrupt database"):
                reader(self.database)
