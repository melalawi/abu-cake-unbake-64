"""Old SQLite generations rebuild at the solver boundary, even with a warm marker."""

import json
import sqlite3
from contextlib import closing
from unittest.mock import patch

from tests.typemap import test_solve_reuse
from unbake import steps
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
                types_db._full.clear()
                if changed:
                    self.source.write_text("int alpha(void) { return 9; }\n")
                calls = self.published.call_count
                revision = types_db.meta(self.database, "revision")
                self.solve()
                self.assertEqual(self.published.call_count, calls + 1)
                self.assertEqual(types_db.meta(self.database, "revision"), revision + 1)
                self.assertIsNotNone(types_db.meta(self.database, "inference_key"))
                self.assertTrue(solver.marker(self.project).is_file())

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
            connection.execute("DROP TABLE redraft")
            connection.execute("DELETE FROM meta WHERE key = 'revision'")
        self.assertEqual(types_db.redrafts(self.database), {})
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
