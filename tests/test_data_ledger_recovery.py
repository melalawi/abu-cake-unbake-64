"""Preserved native SN64 evidence survives bounded storage and source-only recipe regeneration."""

import copy
import json
from dataclasses import replace
from unittest.mock import patch

from tests.test_source_data_build import RECORDS
from tests.test_standalone_data_progress import StandaloneDataProgressTests, records
from unbake import atomic, buildfiles, journal
from unbake.config import Held
from unbake.project import publication_push
from unbake.report import data, progress, verify
from unbake.work import attempts


class DataLedgerRecoveryTests(StandaloneDataProgressTests):
    def capture_recipe(self):
        project = records(self)
        recipe = "".join(
            f"build/%../../../../src/{row['symbol']}.i: PREPROCESS_FLAGS = -Iinclude -DVERSION_US=1\n"
            for row in RECORDS
        )
        (project.root / "units.mk").write_text(recipe)
        proofs = self.admit()["native_data"]
        data.record_producers(project, proofs)
        return project, proofs

    def test_capture_then_source_only_recipe_regeneration_preserves_original_proof_and_local_staleness(self):
        project, proofs = self.capture_recipe()
        before = copy.deepcopy(data.snapshots(project))
        recipe = project.root / "units.mk"
        recipe.write_text(recipe.read_text().replace("build/%../../../../", "build/%/"))
        self.assertEqual(progress.measure(project, None, "us")["measures"]["complete_data"], 33253)
        self.assertEqual(data.snapshots(project), before)
        recipe.write_text(recipe.read_text().replace("-DVERSION_US=1", "-DVERSION_US=2", 1))
        self.assertEqual(progress.measure(project, None, "us")["measures"]["matched_data"], 12792)
        self.assertEqual(data.snapshots(project), before)
        # The stored input identity remains the original capture; no hash rebinding.
        event = attempts.Ledger(project).latest("native.data", proofs[0]["subject"])
        self.assertEqual(event["result"]["value"]["native_data"], proofs[0]["payload"])

    def test_bounded_deduplicated_storage_preserves_events_proofs_merge_and_missing_blob_refusal(self):
        project, proofs = self.capture_recipe()
        history = attempts.Ledger(project)
        history._refresh()
        # Retain an authentic producer proof in repeated historical diagnostic records.
        for index in range(8):
            history.note(
                "diagnostic", str(index), {"proof": proofs[0]}, dependencies=attempts.DependencySet((), {}, {})
            )
        history._refresh()
        expected = copy.deepcopy(history.events)
        original = (project.root / attempts.PATH).read_bytes()
        with patch.object(attempts, "SEGMENT_LIMIT", 512):
            attempts.compact(project)
        stored = (project.root / attempts.PATH).read_bytes()
        reread = attempts.Ledger(project)
        reread._refresh()
        self.assertEqual(reread.events, expected)
        self.assertEqual(progress.measure(project, None, "us")["measures"]["complete_data"], 33253)
        paths = attempts.storage_paths(project)
        self.assertLess(max(path.stat().st_size for path in paths), 16384)
        self.assertLess(len(stored), 2048)
        count = len(paths)
        with patch.object(attempts, "SEGMENT_LIMIT", 512):
            attempts.compact(project)
        self.assertEqual(len(attempts.storage_paths(project)), count)
        merged = attempts.Ledger.merge(original, stored, original, project=project)
        self.assertEqual({row["event_id"]: row for row in attempts.read_records(merged, project)}, expected)
        # Missing and corrupt CAS are evidence failures, never zero-credit success.
        root = json.loads(stored.splitlines()[-1]).get("segment")
        # The final row can be inline when it fits the segment bound.
        if "event" in json.loads(stored.splitlines()[-1]):
            root = json.loads(stored.splitlines()[-1])["event"]["ref"]
        blob = attempts.cache.Cache(project.root / attempts.STORAGE).path("ledger", root)
        content = blob.read_bytes()
        blob.unlink()
        with self.assertRaisesRegex(Held, "blob_missing"):
            attempts.Ledger(project)._refresh()
        atomic.write(blob, content + b" ")
        with self.assertRaisesRegex(Held, "blob_corrupt"):
            attempts.Ledger(project)._refresh()

    def test_ordinary_append_rotates_large_history_and_journal_rollback_keeps_old_proofs(self):
        project, _ = self.capture_recipe()
        before = (project.root / attempts.PATH).read_bytes()
        history = attempts.Ledger(project)
        history._refresh()
        events = copy.deepcopy(history.events)
        with (
            patch.object(attempts, "JOURNAL_LIMIT", 1),
            patch.object(attempts, "SEGMENT_LIMIT", 256),
            self.assertRaisesRegex(RuntimeError, "interrupted"),
            journal.transaction(project),
        ):
            history.note("diagnostic", "new", {"value": 1}, dependencies=attempts.DependencySet((), {}, {}))
            self.assertLess((project.root / attempts.PATH).stat().st_size, 1024)
            raise RuntimeError("interrupted")
        self.assertEqual((project.root / attempts.PATH).read_bytes(), before)
        restored = attempts.Ledger(project)
        restored._refresh()
        self.assertEqual(restored.events, events)
        self.assertEqual(progress.measure(project, None, "us")["measures"]["complete_data"], 33253)

    def test_recovery_on_remote_main_excludes_failed_ancestor_and_retains_source_and_native_proofs(self):
        project, _ = self.capture_recipe()

        # The same transfer used by the owner starts from the clean remote tree.
        history = attempts.Ledger(project)
        history._refresh()
        original = copy.deepcopy(history.events)
        before = (project.root / attempts.PATH).read_bytes()
        recovered = project.root.parent / "recovered"
        current = replace(project, root=recovered)
        current.root.mkdir()
        atomic.write(recovered / attempts.PATH, attempts.stored_history(current, original.values()))
        for row in RECORDS:
            source = project.src / (row["symbol"] + ".c")
            atomic.write(recovered / "src" / source.name, source.read_bytes())
        check = attempts.Ledger(current)
        check._refresh()
        self.assertEqual(check.events, original)
        self.assertEqual((project.root / attempts.PATH).read_bytes(), before)
        # Real Git ancestry is checked separately by the incident evidence script;
        # unit boundaries forbid subprocesses and never invoke native tools.

    def test_concurrent_native_proof_union_stages_new_storage_closure_without_ci_gate(self):
        project, _ = self.capture_recipe()
        base = (project.root / attempts.PATH).read_bytes()
        history = attempts.Ledger(project)
        history.note("diagnostic", "concurrent", {"value": 1}, dependencies=attempts.DependencySet((), {}, {}))
        theirs = (project.root / attempts.PATH).read_bytes()
        atomic.write(project.root / attempts.PATH, base)
        staged = {}

        def git(current, *args):
            if args[0] == "diff":
                return "attempts.jsonl\0"
            if args[0] == "show":
                return {":1:attempts.jsonl": base, ":2:attempts.jsonl": base, ":3:attempts.jsonl": theirs}[
                    args[-1]
                ].decode()
            if args[0] == "add":
                for name in args[2:]:
                    staged[name] = (project.root / name).read_bytes()
                return ""
            raise AssertionError(args)

        with (
            patch.object(publication_push, "_git", side_effect=git),
            patch.object(buildfiles, "write_progress", return_value=[]),
            patch.object(progress, "write", return_value=[]),
            patch.object(verify, "validate") as ci,
            patch.object(attempts, "SEGMENT_LIMIT", 256),
        ):
            self.assertTrue(publication_push.resolve_conflicts(project, self.host))
        ci.assert_not_called()
        expected = {p.relative_to(project.root).as_posix() for p in attempts.storage_paths(project)}
        self.assertTrue(expected <= staged.keys())
        # Read exactly the files that the reconciliation commit stages.
        recovered = project.root.parent / "merged"
        for name, content in staged.items():
            atomic.write(recovered / name, content)
        clean = attempts.Ledger(replace(project, root=recovered))
        clean._refresh()
        self.assertEqual(len(clean.order), 3)
        self.assertEqual({row["event_id"] for row in attempts.read_records(theirs, project)}, set(clean.order))
