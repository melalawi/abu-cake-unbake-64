"""One real RageWars function receipt changes one database row; staged failure preserves the installed DB."""

import copy
from unittest.mock import patch

from tests.typemap.test_types_work_counts import RealSlice
from unbake.typemap import facts, types_db


class DatabaseWorkCounts(RealSlice):
    def setUp(self):
        super().setUp()
        self.database = self.root / "types.sqlite"
        functions = {}
        for row in self.seeds(facts.Store(self.project, self.cache)):
            functions.update(row["functions"])
        self.assertGreater(len(functions), 1)
        self.value = {
            "schema": 1,
            "revision": 1,
            "inference_key": "slice",
            "inference_receipts": "receipts",
            "inference_publication": "headers",
            "functions": functions,
            "globals": {},
            "structs": {},
            "arrays": {},
            "dependencies": {name: [] for name in functions},
        }
        self.summary = {"functions": {name: {"semantic_sha256": "stable", "users": [name]} for name in functions}}

    def publish(self, value):
        staged, digest = types_db.stage(self.database, types_db.encode(value), self.summary)
        types_db.install(self.database, staged)
        return digest

    def writes(self, value):
        adapter = types_db.sqlite if hasattr(types_db, "sqlite") else types_db.sqlite3
        original = adapter.connect
        writes = []

        def connect(*args, **kwargs):
            connection = original(*args, **kwargs)
            connection.set_trace_callback(
                lambda statement: (
                    writes.append(statement)
                    if statement.startswith(("INSERT", "UPDATE", "DELETE"))
                    and "entries" in statement.split("VALUES")[0]
                    else None
                )
            )
            return connection

        with patch.object(adapter, "connect", connect):
            self.publish(value)
        return writes

    def test_one_landing_rewrites_only_the_changed_real_function_receipt(self):
        self.publish(self.value)
        changed = copy.deepcopy(self.value)
        name = next(iter(changed["functions"]))
        changed["functions"][name]["provenance"]["sha256"] = "landed"
        changed["revision"] = 2
        self.assertEqual(len(self.writes(changed)), 1)
        self.assertEqual(types_db.entries(self.database, "functions", changed["functions"]), changed["functions"])
        self.assertEqual(
            types_db.meta(self.database, "content_sha256"), types_db.content_digest(types_db.encode(changed))
        )
        self.assertEqual(types_db.summary(self.database)["functions"], self.summary["functions"])
        self.assertEqual(len(self.writes(changed)), 0)

    def test_deletions_remove_only_the_absent_function_and_its_dependency(self):
        self.publish(self.value)
        changed = copy.deepcopy(self.value)
        name = next(iter(changed["functions"]))
        del changed["functions"][name]
        del changed["dependencies"][name]
        del self.summary["functions"][name]
        self.assertEqual(len(self.writes(changed)), 2)
        self.assertNotIn(name, types_db.read(self.database)["functions"])
        self.assertNotIn(name, types_db.summary(self.database)["functions"])

    def test_a_failed_delta_leaves_the_previous_revision_and_no_staged_file(self):
        self.publish(self.value)
        before = self.database.read_bytes()
        changed = copy.deepcopy(self.value)
        changed["revision"] = 2
        original = getattr(types_db, "_sync", None)
        if original is None:
            self.skipTest("the old full-row publisher has no delta operation")

        def fail(connection, table, *args):
            original(connection, table, *args)
            if table == "entries":
                raise RuntimeError("staging failed")

        with patch.object(types_db, "_sync", fail), self.assertRaisesRegex(RuntimeError, "staging failed"):
            self.publish(changed)
        self.assertEqual(self.database.read_bytes(), before)
        self.assertEqual(list(self.root.glob(".types-*")), [])
