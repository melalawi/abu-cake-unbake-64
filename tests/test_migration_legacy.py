"""Real legacy payloads: migration preserves bytes and unknown evidence explicitly."""

import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from tests.ledger_fixture import history_bytes
from tests.project_fixture import ProjectCase
from unbake import cdecl, config, migrate_state
from unbake.config import Held
from unbake.journal import Journal
from unbake.layout import split
from unbake.process import named
from unbake.report import progress, state
from unbake.typemap import types_db
from unbake.work import attempts

FIXTURE = Path(__file__).parent / "fixtures/migration_legacy"
NAME = "func_800767E4_us"
RAW = (FIXTURE / (NAME + ".c")).read_bytes()
STORE = json.loads((FIXTURE / "storage0.json").read_text())


class LegacyMigrationTests(ProjectCase):
    def source(self):
        for version in self.versions:
            row = self.project.version(version)
            row.split.write_text(row.split.read_text().replace("asm, beta", "asm, " + NAME))
            row.symbols.write_text(row.symbols.read_text().replace("beta", NAME))
        layout = self.project.root / "layout.toml"
        layout.write_bytes(layout.read_bytes().replace(b"beta", NAME.encode()))
        self.project = config.load(self.project.root)
        source = self.project.src / (NAME + ".c")
        source.write_bytes(RAW)
        return source

    def database(self):
        file = types_db.path(self.project)
        with closing(sqlite3.connect(file)) as connection, connection:
            for statement in types_db.SCHEMA:
                connection.execute(statement)
            connection.execute("CREATE TABLE redraft (function TEXT PRIMARY KEY, value TEXT NOT NULL)")
            connection.executemany("INSERT INTO meta VALUES (?,?)", STORE["meta"])
            connection.execute("INSERT INTO entries VALUES (?,?,?)", STORE["entry"])
            connection.execute("INSERT INTO summary VALUES (?,?,?,?)", STORE["summary"])
            connection.execute("INSERT INTO redraft VALUES (?,?)", STORE["redraft"])
        return file

    def test_real_bare_endif_guard_and_nested_variants_capture_exact_body(self):
        self.assertEqual(
            (len(RAW), hashlib.sha256(RAW).hexdigest()),
            (1125, "a3e93bdbe3145327a8e3ea3cbbf2e55051dd9568047752e4fb8261e002258f5a"),
        )
        self.assertEqual(attempts.unguarded(RAW.decode()), RAW.decode().split("\n", 1)[1].rsplit("#endif", 1)[0])
        body = "int beta(void) {\n#if VERSION_US\nreturn 1;\n#else\nreturn 2;\n#endif\n}\n"
        for start in ("#ifdef NON_MATCHING", "#if defined(NON_MATCHING)", "#if defined NON_MATCHING"):
            text = "/* preface */\n" + start + "\n" + body + "#endif /* optional comment */\n"
            self.assertEqual(attempts.unguarded(text), body)
        self.assertEqual(attempts.unguarded(attempts.guarded(body)), body)

    def test_lost_unbalanced_outer_else_and_fake_guards_refuse(self):
        bad = [
            RAW.decode().rsplit("#endif", 1)[0],
            RAW.decode() + "int leaked;\n",
            RAW.decode().replace("#endif\n", "#else\nint leaked;\n#endif\n"),
            RAW.decode().replace("#endif\n", "#endif extra\n"),
            "/* #ifdef NON_MATCHING */\nint beta(void) {return 0;}\n",
            "#ifdef NON_MATCHING\n#if X\n#else\n#else\n#endif\n#endif\n",
            "#ifdef NON_MATCHING\n#if X\n#else\n#elif Y\n#endif\n#endif\n",
        ]
        for text in bad:
            with self.subTest(text=text[-60:]), self.assertRaises(Held) as caught:
                attempts.unguarded(text)
            self.assertEqual(caught.exception.key, "fuzzy.source.guard")

    def test_public_migration_retains_real_source_without_fabricated_publication(self):
        source = self.source()
        with patch("unbake.process.run_native", side_effect=AssertionError("native work")) as native:
            planned = migrate_state.plan(self.project)
            with (
                patch("unbake.report.state.inventory", wraps=state.inventory) as inventory,
                patch("unbake.layout.split.functions", wraps=split.functions) as memberships,
                patch("unbake.cdecl.declarations", wraps=cdecl.declarations) as parses,
            ):
                result = migrate_state.apply(self.project, planned)
            self.assertEqual((memberships.call_count, parses.call_count), (2 * len(self.versions), 1))
            self.assertEqual(
                (inventory.call_count, native.call_count, result["events"], result["native_calls"]), (1, 0, 1, 0)
            )
        history = attempts.Ledger(self.project)
        self.assertEqual(source.read_bytes(), RAW)
        self.assertEqual((history.summaries(), history.publication(NAME), history.fuzzy(NAME)), ({}, None, None))
        event = next(iter(history.events.values()))
        self.assertEqual(
            (event["kind"], event["result"]["state"], event["result"]["proof_ids"]), ("source.retained", "ok", [])
        )
        receipt = history.fuzzy_sources()[NAME]
        self.assertEqual((receipt["score"], receipt["versions"]), (None, {v: None for v in self.versions}))
        current = state.inventory(self.project)
        report = progress.measure(self.project, self.host, self.versions[0], current=current)
        self.assertEqual(report["measures"]["matched_code"], 0)
        self.assertNotIn("fuzzy_match_percent", next(u for u in report["units"] if NAME in u["name"])["functions"][0])

    def test_known_storage0_plan_conversion_backup_rows_and_readback(self):
        database = self.database()
        before = database.read_bytes()
        with patch("unbake.process.run_native", side_effect=AssertionError("native work")) as native:
            planned = migrate_state.plan(self.project)
            self.assertEqual(planned["counts"]["files"], 1)
            result = migrate_state.apply(self.project, planned)
            self.assertEqual((result["events"], result["native_calls"], native.call_count), (1, 0, 0))
        self.assertEqual((self.project.root / result["backup"] / "build/types.sqlite").read_bytes(), before)
        with closing(sqlite3.connect(database)) as connection, connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 2)
            self.assertEqual(connection.execute("SELECT * FROM entries").fetchall(), [tuple(STORE["entry"])])
            self.assertEqual(connection.execute("SELECT * FROM summary").fetchall(), [tuple(STORE["summary"])])
            self.assertEqual(
                connection.execute("SELECT count(*) FROM sqlite_master WHERE name='redraft'").fetchone()[0], 0
            )
            metadata = dict(connection.execute("SELECT key,value FROM meta"))
        self.assertEqual(metadata, {**dict(STORE["meta"]), "migration_reuse": '"unverified"'})
        self.assertFalse(types_db.compatible(database))
        self.assertEqual(
            attempts.Ledger(self.project).redrafts(), {STORE["redraft"][0]: json.loads(STORE["redraft"][1])}
        )
        ledger = (self.project.root / attempts.PATH).read_bytes()
        self.assertEqual(
            migrate_state.apply(self.project, migrate_state.plan(self.project)), {"reused": True, "events": 0}
        )
        self.assertEqual((self.project.root / attempts.PATH).read_bytes(), ledger)

    def test_storage0_intake_is_seven_bounded_metadata_queries_without_entry_scan(self):
        database = self.database()
        statements = []
        connect = types_db._connect

        def traced(file):
            connection = connect(file)
            connection.set_trace_callback(statements.append)
            return connection

        with patch.object(types_db, "_connect", side_effect=traced):
            self.assertEqual(types_db.legacy_storage(database), 0)
        self.assertEqual(len(statements), 7)
        self.assertFalse(any("FROM entries" in s or "FROM summary" in s or "FROM redraft" in s for s in statements))

    def test_unknown_legacy_shapes_metadata_and_versions_do_not_write(self):
        database = self.database()
        original = database.read_bytes()
        for change in (
            "DROP TABLE summary",
            "ALTER TABLE entries ADD COLUMN mystery TEXT",
            "CREATE TABLE mystery(value TEXT)",
            "DELETE FROM meta WHERE key='schema'",
            "UPDATE meta SET value='true' WHERE key='schema'",
            "PRAGMA user_version=7",
            "UPDATE meta SET value='-1' WHERE key='revision'",
            "UPDATE meta SET value='\\\"bad\\\"' WHERE key='solution_sha256'",
        ):
            database.write_bytes(original)
            with closing(sqlite3.connect(database)) as connection, connection:
                connection.execute(change)
            before = database.read_bytes()
            with self.subTest(change=change), self.assertRaises(Held):
                migrate_state.plan(self.project)
            self.assertEqual(database.read_bytes(), before)
            self.assertFalse((self.project.root / attempts.PATH).exists())

    def test_already_migrated_history_is_preserved_and_retention_appended_once(self):
        self.source()
        summary = attempts.Summary(12, {"us": 70}, False, 2.0, 3, None, (3, 4))
        target = self.project.root / attempts.PATH
        target.write_bytes(history_bytes(self.project, {"alpha": summary}))
        prefix = target.read_bytes()
        with patch("unbake.report.state.inventory", wraps=state.inventory) as inventory:
            self.assertEqual(
                migrate_state.apply(self.project, migrate_state.plan(self.project)), {"reused": True, "events": 1}
            )
            complete = target.read_bytes()
            self.assertEqual(
                migrate_state.apply(self.project, migrate_state.plan(self.project)), {"reused": True, "events": 0}
            )
        self.assertEqual((inventory.call_count, target.read_bytes()), (2, complete))
        self.assertTrue(complete.startswith(prefix))
        self.assertEqual(attempts.Ledger(self.project).summaries()["alpha"], summary)
        self.assertFalse((self.project.root / "attempts.json").exists())
        self.assertEqual(len(complete.splitlines()), len(prefix.splitlines()) + 1)

    def test_failed_inventory_rolls_back_and_preserves_verified_backup(self):
        database = self.database()
        before = database.read_bytes()
        planned = migrate_state.plan(self.project)
        with (
            patch(
                "unbake.report.state.inventory",
                side_effect=Held(named("source.hash", "fixture hash refusal", owner="report.state", stage="report")),
            ),
            self.assertRaises(Held),
        ):
            migrate_state.apply(self.project, planned)
        self.assertEqual(database.read_bytes(), before)
        self.assertEqual((self.project.root / planned["backup"] / "build/types.sqlite").read_bytes(), before)
        self.assertFalse((self.project.root / attempts.PATH).exists())
        self.assertFalse((self.project.build / "migration.journal").exists())

    def test_interrupted_apply_requires_public_recovery_then_new_reviewed_plan(self):
        legacy = self.project.root / "attempts.json"
        legacy.write_text(json.dumps({"v": 1, "functions": {}}))
        old = legacy.read_bytes()
        target = self.project.root / attempts.PATH
        journal = Journal(self.project.build / "migration.journal", root=self.project.root)
        journal.__enter__()
        journal.save([legacy, target])
        legacy.unlink()
        target.write_bytes(b"incomplete")
        journal.recording.__exit__(None, None, None)
        from unbake import journal as journal_owner

        journal_owner._current.reset(journal.token)
        with self.assertRaisesRegex(Held, "--recover"):
            migrate_state.plan(self.project)
        self.assertEqual(migrate_state.recover(self.project), {"recovered_files": 2, "native_calls": 0})
        self.assertEqual(legacy.read_bytes(), old)
        self.assertFalse(target.exists())
        self.assertEqual(migrate_state.apply(self.project, migrate_state.plan(self.project))["events"], 0)

    def test_changed_retained_identity_is_not_silently_reimported(self):
        source = self.source()
        migrate_state.apply(self.project, migrate_state.plan(self.project))
        target = self.project.root / attempts.PATH
        before = target.read_bytes()
        source.write_bytes(RAW.replace(b"0x7D0", b"0x7D1"))
        with self.assertRaises(Held) as caught:
            migrate_state.apply(self.project, migrate_state.plan(self.project))
        self.assertEqual(caught.exception.key, "source.hash")
        self.assertEqual(target.read_bytes(), before)

    def test_source_pins_changed_since_plan_refuse_without_writes(self):
        source = self.source()
        planned = migrate_state.plan(self.project)
        source.write_bytes(RAW + b"/* changed */\n")
        with self.assertRaisesRegex(Held, "inputs changed"):
            migrate_state.apply(self.project, planned)
        self.assertFalse((self.project.root / attempts.PATH).exists())


class StaleReceiptTests(ProjectCase):
    def test_every_stale_receipt_is_reported_in_one_pass_and_matching_ones_stay(self):
        digest = hashlib.sha256
        (self.project.src / "alpha.c").write_bytes(b"int a;\n")
        (self.project.src / "beta.c").write_bytes(b"int b;\n")
        (self.project.src / "gamma.c").write_bytes(b"int c;\n")
        receipts = {
            "alpha": {"source_sha256": digest(b"int a;\n").hexdigest()},
            "beta": {"source_sha256": digest(b"older body\n").hexdigest()},
            "gamma": {"source_sha256": digest(b"older body\n").hexdigest()},
            "removed": {"source_sha256": "0" * 64},
        }
        fresh, stale = migrate_state.current_receipts(self.project, receipts, set(receipts))
        self.assertEqual((sorted(fresh), stale), (["alpha", "removed"], ["beta", "gamma"]))
