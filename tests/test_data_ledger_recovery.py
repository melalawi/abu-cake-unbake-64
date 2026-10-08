"""Preserved native SN64 evidence survives bounded storage and source-only recipe regeneration."""

import copy
import hashlib
import io
import json
import shutil
import sys
import types
import zipfile
from dataclasses import replace
from unittest.mock import patch

from tests.test_source_data_build import RECORDS
from tests.test_standalone_data_progress import StandaloneDataProgressTests, records
from unbake import atomic, buildfiles, journal, land, process
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

    def legacy_linker_proofs(self):
        project = records(self)
        symbols = project.version("us").symbols
        symbols.write_text("Sn64DefinitionHeaderTail = 0x80002008;\n")
        (project.root / "versions/us/symbols.ld").write_text(buildfiles.symbols_ld(project, "us"))
        proofs = self.admit()["native_data"]
        for proof in proofs:
            # Retain the actual captured native payload in its pre-projection format.
            proof["dependencies"]["values"].pop("producer_linker_inputs")
            proof["payload"]["evidence"][0]["input_identity"] = attempts.dependency_record(proof["dependencies"]).digest
        data.record_producers(project, proofs)
        archive = project.root.parent / "archived-inputs"
        for proof in proofs:
            for pin in attempts.dependency_record(proof["dependencies"]).files:
                path = project.root.joinpath(*pin.path.parts)
                if path.is_file() and path.suffix in {".c", ".h", ".s", ".inc", ".ld", ".txt"}:
                    target = archive.joinpath(*pin.path.parts)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(path, target)
        return project, archive

    def test_hash_verified_archived_linker_projection_serializes_actual_data_without_rebinding(self):
        project, archive = self.legacy_linker_proofs()
        history = attempts.ledger(project)
        history._refresh()
        original = copy.deepcopy(history.events)
        symbols = project.version("us").symbols
        symbols.write_text(symbols.read_text() + "unrelated_delivery = 0x80002000;\n")
        (project.root / "versions/us/symbols.ld").write_text(buildfiles.symbols_ld(project, "us"))
        self.assertEqual(progress.measure(project, None, "us")["measures"]["complete_data"], 0)
        with journal.transaction(project):
            self.assertEqual(len(data.record_input_projections(project, archive)), 2)
        self.assertEqual(data.record_input_projections(project, archive), [])
        buildfiles.write_progress(project, publish_branch="main")
        (project.root / "README.md").write_text("# Fixture\n\n## Progress\n\n| us (US fixture) |\n|---|\n\n## Build\n")
        with patch.object(progress, "owner_descriptions", return_value={"us": "US fixture"}):
            progress.write(project, None, source_only=True)
        serialized = json.loads((project.root / "versions/us/report.json").read_text())
        self.assertEqual(serialized["measures"]["matched_data"], 33253)
        self.assertEqual(serialized["measures"]["complete_data"], 33253)
        history._refresh()
        self.assertTrue(all(history.events[key] == event for key, event in original.items()))
        # A relevant symbol edit invalidates only its owning actual native record.
        symbols.write_text(symbols.read_text().replace("0x80002008", "0x80002004"))
        self.assertEqual(progress.measure(project, None, "us")["measures"]["complete_data"], 0)
        symbols.write_bytes((archive / "versions/us/symbol_addrs.txt").read_bytes())
        header = project.include[-1] / "sn64_type_records.h"
        header.write_text(header.read_text() + "\n#define CHANGED_INPUT 1\n")
        self.assertEqual(progress.measure(project, None, "us")["measures"]["complete_data"], 0)

    def test_archived_projection_refuses_wrong_original_bytes_and_keeps_genuine_recipe_stale(self):
        project, archive = self.legacy_linker_proofs()
        symbols = archive / "versions/us/symbols.ld"
        original = symbols.read_bytes()
        symbols.write_bytes(original + b"\n")
        with self.assertRaisesRegex(Held, "archived linker input"):
            data.record_input_projections(project, archive)
        symbols.write_bytes(original)
        source = archive / "src" / (RECORDS[0]["symbol"] + ".c")
        original_source = source.read_bytes()
        source.write_bytes(original_source + b"\n")
        with self.assertRaisesRegex(Held, "archived producer input"):
            data.record_input_projections(project, archive)
        source.write_bytes(original_source)
        data.record_input_projections(project, archive)
        recipe = project.root / "units.mk"
        recipe.write_text(
            recipe.read_text() + f"build/%/src/{RECORDS[0]['symbol']}.i: PREPROCESS_FLAGS = -DCHANGED=1\n"
        )
        self.assertEqual(progress.measure(project, None, "us")["measures"]["complete_data"], RECORDS[1]["bytes"])
        source = project.src / (RECORDS[1]["symbol"] + ".c")
        source.write_text(source.read_text() + "\n/* changed source input */\n")
        self.assertEqual(progress.measure(project, None, "us")["measures"]["complete_data"], 0)

    def test_first_ordinary_rotation_commits_real_canonical_reader_with_decodable_native_history(self):
        project = records(self)
        staged, committed = {}, {}
        head = "head"

        def git(current, *args):
            nonlocal head
            if args[0] == "rev-parse":
                return "base" if args[-1] == "FETCH_HEAD" else head
            if args[0] == "merge-base":
                return "base"
            if args[0] == "add":
                staged.update({name: (current.root / name).read_bytes() for name in args[2:]})
            elif args[0] == "commit":
                committed.update(staged)
                head = "committed"
            elif args[0] == "push":
                self.assertEqual(args[-1], "committed:refs/heads/" + self.host.publish_branch)
            elif args[0] in {"fetch", "status", "ls-files", "check-ref-format"}:
                pass
            else:
                return self.git(current, *args)
            return ""

        with (
            patch.object(publication_push, "_git", side_effect=git),
            patch.object(verify, "validate") as ci,
            patch.object(attempts, "JOURNAL_LIMIT", 1),
            patch.object(attempts, "SEGMENT_LIMIT", 256),
        ):
            result = publication_push.push(project, self.host, "origin")
        ci.assert_not_called()
        self.assertEqual(result["head"], "committed")
        self.assertTrue({verify.BUNDLE, ".github/workflows/progress.yml", ".gitlab-ci.yml"} <= committed.keys())
        self.assertEqual(json.loads(committed[attempts.PATH].splitlines()[0])["schema"], 3)
        bundle_hash = hashlib.sha256(committed[verify.BUNDLE]).hexdigest()
        self.assertIn(bundle_hash, committed[".github/workflows/progress.yml"].decode())
        recovered = project.root.parent / "first-committed"
        for name, content in committed.items():
            atomic.write(recovered / name, content)
        # Execute the actual reader member in the committed generated bundle,
        # rather than substituting the checkout's Ledger class or mocking generation.
        with zipfile.ZipFile(io.BytesIO(committed[verify.BUNDLE])) as bundle:
            source = bundle.read("unbake/work/attempts.py")
        name = "unbake.work.committed_attempts"
        module = types.ModuleType(name)
        module.__file__ = str(recovered / verify.BUNDLE) + "/unbake/work/attempts.py"
        with patch.dict(sys.modules, {name: module}):
            exec(compile(source, module.__file__, "exec"), module.__dict__)
            reader = module.Ledger(replace(project, root=recovered))
            reader._refresh()
            expected = attempts.Ledger(project)
            expected._refresh()
            self.assertEqual(reader.events, expected.events)
            self.assertEqual(len(reader.order), 2)

    def test_large_git_storage_closure_uses_nul_stdin_for_publication_and_atomic_land_commit(self):
        names = [f"history/ledger/{i:02x}/" + "a" * 64 for i in range(34896)]
        names.extend(["src/name with spaces.c", "src/name\nwith newline.c"])
        seen = []

        def run(argv, work, phase, **kwargs):
            self.assertLess(sum(len(arg.encode()) + 9 for arg in argv), 32768)
            self.assertEqual(kwargs["stdin"].split("\0")[:-1], names)
            self.assertIn("--pathspec-from-file=-", argv)
            self.assertIn("--pathspec-file-nul", argv)
            seen.append(phase)
            return process.NativeResult(
                tuple(argv), str(work), 0, None, "", "", "native-exit", None, "utf-8", "strict", {}
            )

        with patch.object(process, "run_native", side_effect=run):
            publication_push._git(self.project, "add", "--", *names)
            land._git(self.project, "add", "--", *names)
            land._git(self.project, "commit", "--only", "-m", "packed history", "--", *names)
        self.assertEqual(seen, ["publish", "land", "land"])
        batches = list(process.path_batches(names))
        self.assertEqual([name for batch in batches for name in batch], names)
        self.assertTrue(all(sum(len(name.encode()) + 9 for name in batch) <= 32768 for batch in batches))
