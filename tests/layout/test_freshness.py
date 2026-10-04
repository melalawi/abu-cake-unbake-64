"""Layout rewrites preserve exact receipts while refusing authored changes."""

import copy
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from unbake.layout import apply, freshness, index, map
from unbake.project.config import Held
from unbake.typemap import storage


class FreshnessTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=os.environ["TMPDIR"])
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.project = SimpleNamespace(
            root=root,
            src=root / "src",
            include=(root / "include",),
            build=root / "build",
            id="project",
            workspace_id="workspace",
            versions=(),
        )
        self.project.src.mkdir()
        self.project.include[0].mkdir()
        self.source = self.project.src / "first.c"
        self.original = b"int first(void) { return 1; }\n"
        self.source.write_bytes(self.original)
        self.ownership = map.Map(2, (map.Group("one", "span", "default", ("first",)),))
        self.lookup = {"schema": 1, "symbols": {}, "clusters": {}, "headers": {"span/one.h": "a" * 64}}
        self.outputs = {
            self.project.include[0] / "span/one.h": b"",
            index.path(self.project): index.encoded(self.lookup),
            self.source: b'#include "span/one.h"\n' + self.original,
        }
        self.receipt = {
            **storage.identity(self.project),
            "records": {
                "first": {
                    "source": "src/first.c",
                    "source_sha256": storage.digest(self.original),
                    "proof": {"matched": True, "source_sha256": storage.digest(self.original)},
                }
            },
        }
        storage.write(self.project.build / "types/proven.json", storage.encoded(self.receipt))
        self.value = {"inputs_sha256": {}, "rendered_sha256": {}}
        storage.write(self.project.build / "types/database.json", storage.encoded(self.value))

    def prepare(self):
        return freshness.prepare(
            self.project,
            {self.source: self.source.read_text()},
            self.outputs,
            self.ownership,
            self.lookup,
            set(),
        )

    def test_fresh_receipt_rekeys_output_and_retains_original_proof(self):
        receipts = self.prepare()
        self.assertEqual(receipts["records"]["first"]["source_sha256"], storage.digest(self.outputs[self.source]))
        self.assertEqual(receipts["records"]["first"]["proof"], self.receipt["records"]["first"]["proof"])
        self.assertEqual(json.loads((self.project.build / "types/proven.json").read_bytes()), self.receipt)

    def test_retained_blob_is_authenticated_and_exact_rewrite_replayed(self):
        listing = subprocess.CompletedProcess([], 0, "abc src/first.c\n", "")
        reader = MagicMock()
        reader.__enter__.return_value = reader
        reader.stdin = io.BytesIO()
        reader.stdout = io.BytesIO(b"abc blob " + str(len(self.original)).encode() + b"\n" + self.original + b"\n")
        with (
            patch.object(subprocess, "run", return_value=listing),
            patch.object(subprocess, "Popen", return_value=reader),
        ):
            retained = freshness.retained(self.project, {storage.digest(self.original)})
        self.assertEqual(retained, {storage.digest(self.original): self.original})
        mock = patch.object(freshness, "retained", return_value=retained)
        mock.start()
        self.addCleanup(mock.stop)
        self.source.write_bytes(self.outputs[self.source])
        self.assertIsNotNone(self.prepare())
        self.source.write_bytes(self.outputs[self.source] + b"/* real edit */\n")
        with self.assertRaisesRegex(Held, "published source changed"):
            self.prepare()
        self.source.write_bytes(self.outputs[self.source].replace(b"return 1", b"return 2"))
        with self.assertRaisesRegex(Held, "published source changed"):
            self.prepare()

    def test_equal_local_declarations_normalize_but_changed_type_is_refused(self):
        old = b"typedef struct Item { int value; } Item;\nint first(void) { return 1; }\n"
        current = b'#include "span/one.h"\ntypedef struct Item Item;\nint first(void) { return 1; }\n'
        header = self.project.include[0] / "span/one.h"
        header.parent.mkdir()
        header.write_bytes(b"typedef struct Item { int value; } Item;\n")
        self.outputs[header] = header.read_bytes()
        self.outputs[self.source] = current
        self.source.write_bytes(current)
        self.receipt["records"]["first"]["source_sha256"] = storage.digest(old)
        storage.write(self.project.build / "types/proven.json", storage.encoded(self.receipt))
        with patch.object(freshness, "retained", return_value={storage.digest(old): old}):
            self.assertIsNotNone(self.prepare())
            changed = current.replace(b"typedef struct Item Item;", b"typedef struct Item { float value; } Item;")
            self.source.write_bytes(changed)
            with self.assertRaises(Held):
                self.prepare()

    def test_generated_forward_alias_of_imported_tag_is_normalized(self):
        old = b"int first(void) { return 1; }\n"
        current = b'#include "span/one.h"\ntypedef struct Owner Owner;\n' + old
        header = self.project.include[0] / "span/one.h"
        header.parent.mkdir()
        header.write_bytes(b"struct Owner { int value; };\n")
        self.outputs[header] = header.read_bytes()
        self.outputs[self.source] = current
        self.source.write_bytes(current)
        with patch.object(freshness, "retained", return_value={storage.digest(old): old}):
            self.assertIsNotNone(self.prepare())
            self.source.write_bytes(current.replace(b"Owner Owner", b"Other Owner"))
            with self.assertRaises(Held):
                self.prepare()

    def test_generated_unknown_carrier_preserves_width_and_signedness(self):
        current = b'#include "span/one.h"\ntypedef s32 M2C_UNK;\n' + self.original
        header = self.project.include[0] / "span/one.h"
        header.parent.mkdir()
        header.write_bytes(b"typedef int s32;\ntypedef unsigned int u32;\n")
        self.outputs[header] = header.read_bytes()
        self.outputs[self.source] = current
        self.source.write_bytes(current)
        with patch.object(freshness, "retained", return_value={storage.digest(self.original): self.original}):
            self.assertIsNotNone(self.prepare())
            self.source.write_bytes(current.replace(b"typedef s32", b"typedef u32"))
            with self.assertRaises(Held):
                self.prepare()

    def test_retained_shared_import_rehome_preserves_other_import_edits(self):
        old = b'#include "shared/sdk.h"\n' + self.original
        current = b'#include "sdk.h"\n' + self.original
        header = self.project.include[0] / "sdk.h"
        header.write_bytes(b"typedef int word;\n")
        self.outputs[header] = header.read_bytes()
        self.outputs[self.source] = current
        self.source.write_bytes(current)
        self.receipt["records"]["first"]["source_sha256"] = storage.digest(old)
        storage.write(self.project.build / "types/proven.json", storage.encoded(self.receipt))
        with patch.object(freshness, "retained", return_value={storage.digest(old): old}):
            self.assertIsNotNone(self.prepare())
            self.source.write_bytes(current.replace(b'"sdk.h"', b'"unrelated.h"'))
            with self.assertRaises(Held):
                self.prepare()

    def test_legacy_replay_preserves_body_and_comment_bytes(self):
        old = (
            '#include "types.h"\n\n#include "span/one.h"\n/* authored */\n'
            "extern struct Item { int x; } *value;\nint first(void) { return value->x; }\n"
        )
        current = (
            '#include "types.h"\n/* authored */\n'
            "extern struct Item_first { int x; } *value;\nint first(void) { return value->x; }\n"
        )
        replay = freshness._legacy_replay(old, current, {"span/one.h"}, "first")
        self.assertEqual(replay, current)
        for edit in (
            current.replace("authored", "edited"),
            current.replace("return value->x", "return 2"),
            current.replace("int x", "float x"),
        ):
            self.assertNotEqual(freshness._legacy_replay(old, edit, {"span/one.h"}, "first"), edit)

    def test_refresh_does_not_classify_existing_authored_headers_as_generated(self):
        header = self.project.include[0] / "types.h"
        header.write_text("typedef int s32;\n")
        value = {"inputs_sha256": {"include/types.h": storage.file_digest(header)}, "rendered_sha256": {}}
        with (
            patch.object(map, "load", return_value=self.ownership),
            patch.object(index, "load", return_value=self.lookup),
            patch.object(storage, "inputs", return_value=value["inputs_sha256"]),
        ):
            _, previous = freshness.refresh(self.project, None, value)
        self.assertNotIn("types.h", previous)

    def test_comments_in_equal_declarations_remain_byte_sensitive(self):
        old = b"typedef struct Item { int value; /* authored */ } Item;\n" + self.original
        current = b'#include "span/one.h"\n' + old
        header = self.project.include[0] / "span/one.h"
        header.parent.mkdir()
        header.write_bytes(b"typedef struct Item { int value; } Item;\n")
        self.outputs[header] = header.read_bytes()
        self.outputs[self.source] = current
        self.receipt["records"]["first"]["source_sha256"] = storage.digest(old)
        storage.write(self.project.build / "types/proven.json", storage.encoded(self.receipt))
        with patch.object(freshness, "retained", return_value={storage.digest(old): old}):
            self.source.write_bytes(current)
            self.assertIsNotNone(self.prepare())
            for edit in (current.replace(b"authored", b"edited"), current.replace(b"/* authored */", b"")):
                self.source.write_bytes(edit)
                with self.assertRaisesRegex(Held, "published source changed"):
                    self.prepare()

    def test_missing_retained_bytes_refuse_even_import_shaped_edits(self):
        self.source.write_bytes(self.outputs[self.source])
        with (
            patch.object(freshness, "retained", return_value={}),
            self.assertRaisesRegex(Held, "published source changed"),
        ):
            self.prepare()

    def publish(self, ok=True):
        receipts = self.prepare()
        with (
            patch("unbake.project.build.build", return_value={"us": SimpleNamespace(ok=ok)}),
            patch("unbake.typemap.storage.inputs", return_value={"layout.toml": "new", "new-input": "new"}),
        ):
            return freshness.publish(
                self.project,
                SimpleNamespace(),
                copy.deepcopy(self.value),
                self.outputs,
                {self.source: self.source.read_text()},
                receipts,
            )

    def test_success_refreshes_complete_input_set_and_real_edit_remains_stale(self):
        self.publish()
        value = json.loads((self.project.build / "types/database.json").read_bytes())
        self.assertEqual(value["inputs_sha256"], {"layout.toml": "new", "new-input": "new"})
        self.assertIsNone(storage.changed_source(self.project))
        self.source.write_bytes(self.source.read_bytes() + b"/* changed */\n")
        self.assertEqual(storage.changed_source(self.project), self.source)

    def test_failed_proof_rolls_back_sources_headers_and_receipts(self):
        before = (self.project.build / "types/proven.json").read_bytes()
        with self.assertRaisesRegex(Held, "layout.proof"):
            self.publish(ok=False)
        self.assertEqual(self.source.read_bytes(), self.original)
        self.assertEqual((self.project.build / "types/proven.json").read_bytes(), before)
        self.assertFalse(index.path(self.project).exists())
        self.assertFalse((self.project.include[0] / "span/one.h").exists())

    def test_publication_keeps_semantic_summary_and_redraft_identity_current(self):
        summary = self.project.build / "types/summary.json"
        redraft = self.project.build / "types/redraft.json"
        storage.write(
            summary,
            storage.encoded(
                {**storage.identity(self.project), "database_sha256": "old", "functions": {"first": "same"}}
            ),
        )
        storage.write(
            redraft,
            storage.encoded({**storage.identity(self.project), "functions": {"first": {"type_db_sha256": "old"}}}),
        )
        self.publish()
        digest = storage.file_digest(self.project.build / "types/database.json")
        self.assertEqual(json.loads(summary.read_bytes())["database_sha256"], digest)
        self.assertEqual(json.loads(summary.read_bytes())["functions"], {"first": "same"})
        self.assertEqual(json.loads(redraft.read_bytes())["functions"]["first"]["type_db_sha256"], digest)

    def test_split_units_are_applied_before_refresh_and_publication(self):
        from contextlib import nullcontext

        order = []
        with (
            patch.object(map, "load", return_value=self.ownership),
            patch("unbake.typemap.database.load", return_value=self.value),
            patch("unbake.project.build.lock", return_value=nullcontext()),
            patch.object(freshness, "transaction", return_value=nullcontext()),
            patch.object(apply, "units", side_effect=lambda project: order.append("units") or 1),
            patch.object(
                freshness, "refresh", side_effect=lambda *args: (order.append("refresh") or self.value, set())
            ),
            patch.object(apply, "_run", side_effect=lambda *args, **kwargs: order.append("publish") or 2),
        ):
            self.assertEqual(apply.run(self.project), 3)
        self.assertEqual(order, ["units", "refresh", "publish"])

    def test_authored_header_edit_cannot_be_refreshed_by_layout(self):
        header = self.project.include[0] / "sdk.h"
        header.write_bytes(b"changed")
        self.value["inputs_sha256"]["include/sdk.h"] = storage.digest(b"old")
        with self.assertRaisesRegex(Held, "input changed: include/sdk.h"):
            self.publish()
        self.assertEqual(self.source.read_bytes(), self.original)

    def test_retained_draft_is_authenticated_without_git(self):
        folder = self.project.build / "drafts"
        folder.mkdir()
        (folder / "original.c").write_bytes(self.original)
        (folder / "untrusted.c").write_bytes(b"other source")
        with patch.object(subprocess, "run") as git:
            self.assertEqual(
                freshness.retained(self.project, {storage.digest(self.original)}),
                {storage.digest(self.original): self.original},
            )
        git.assert_not_called()

    def test_index_recovery_uses_only_ownership_destinations(self):
        owned = self.project.include[0] / "span/one.h"
        owned.parent.mkdir()
        owned.write_text("typedef struct Item Item;\nextern int first(void);\n")
        (self.project.include[0] / "authored.h").write_text("extern int authored;\n")
        lookup = freshness.recover_index(self.project, self.ownership)
        self.assertEqual(lookup["headers"], {"span/one.h": storage.file_digest(owned)})
        self.assertEqual(lookup["symbols"]["first"], "span/one.h")
        self.assertEqual(lookup["type_headers"]["Item"], ["span/one.h"])
        self.assertNotIn("authored", lookup["symbols"])

    def test_index_recovery_includes_data_only_symbol_segments(self):
        self.project.versions = ("us",)
        header = self.project.include[0] / "data_span/data.h"
        header.parent.mkdir()
        header.write_text("extern int data_symbol;\n")
        with patch("unbake.typemap.database.symbol_segments", return_value={"data_symbol": "data_span"}):
            lookup = freshness.recover_index(self.project, self.ownership)
        self.assertEqual(lookup["symbols"]["data_symbol"], "data_span/data.h")
        self.assertEqual(lookup["headers"]["data_span/data.h"], storage.file_digest(header))

    def test_refresh_replays_cutover_and_defers_cartridge_proof_to_publication(self):
        header = self.project.include[0] / "span/one.h"
        header.parent.mkdir()
        header.write_bytes(b"")
        folder = self.project.build / "drafts"
        folder.mkdir()
        (folder / "original.c").write_bytes(self.original)
        self.source.write_bytes(self.outputs[self.source])
        solved = {"inputs_sha256": {"new": "pin"}}
        with (
            patch.object(map, "load", return_value=self.ownership),
            patch.object(freshness, "recover_index", return_value=self.lookup),
            patch.object(storage, "inputs", side_effect=Held("solve", "types.feedback.source_sha256: changed")),
            patch("unbake.project.build.build") as build,
            patch("unbake.typemap.solver.solve", return_value=solved) as solve,
        ):
            result, _ = freshness.refresh(self.project, SimpleNamespace(), self.value)
        build.assert_not_called()
        self.assertEqual(result, solved)
        solve.assert_called_once()
        self.assertIsNone(storage.changed_source(self.project))
        self.assertTrue(index.path(self.project).is_file())

    def test_failed_final_proof_rolls_back_provisional_refresh_receipts(self):
        from contextlib import nullcontext

        header = self.project.include[0] / "span/one.h"
        header.parent.mkdir()
        header.write_bytes(b"")
        before = (self.project.build / "types/proven.json").read_bytes()
        folder = self.project.build / "drafts"
        folder.mkdir()
        (folder / "original.c").write_bytes(self.original)
        self.source.write_bytes(self.outputs[self.source])
        with (
            patch.object(map, "load", return_value=self.ownership),
            patch("unbake.typemap.database.load", return_value=self.value),
            patch("unbake.project.build.lock", return_value=nullcontext()),
            patch.object(freshness, "recover_index", return_value=self.lookup),
            patch.object(storage, "inputs", side_effect=Held("solve", "types.feedback.source_sha256: changed")),
            patch("unbake.typemap.solver.solve", return_value=self.value),
            patch.object(apply, "_run", side_effect=Held("layout", "layout.proof: failed")),
            self.assertRaisesRegex(Held, "layout.proof"),
        ):
            apply.run(self.project, SimpleNamespace())
        self.assertEqual((self.project.build / "types/proven.json").read_bytes(), before)
        self.assertEqual(self.source.read_bytes(), self.outputs[self.source])

    def test_transaction_restores_deleted_headers_and_removes_new_outputs(self):
        old = self.project.include[0] / "old.h"
        new = self.project.include[0] / "new.h"
        old.write_bytes(b"old")
        timestamp = old.stat().st_mtime_ns
        with self.assertRaisesRegex(ValueError, "refused"), freshness.transaction(self.project):
            old.unlink()
            new.write_bytes(b"new")
            self.source.write_bytes(b"changed")
            raise ValueError("refused")
        self.assertEqual(old.read_bytes(), b"old")
        self.assertEqual(old.stat().st_mtime_ns, timestamp)
        self.assertFalse(new.exists())
        self.assertEqual(self.source.read_bytes(), self.original)

    def test_dry_run_does_not_build_or_publish_receipts(self):
        with (
            patch("unbake.typemap.database.load", return_value=self.value),
            patch("unbake.typemap.database._render", return_value=self.outputs),
            patch(
                "unbake.typemap.regeneration.Session",
                return_value=SimpleNamespace(sources={self.source: self.source.read_text()}, ownership=self.ownership),
            ),
            patch.object(map, "load", return_value=self.ownership),
            patch("unbake.project.build.build") as proof,
        ):
            apply.run(self.project, dry_run=True)
        proof.assert_not_called()
        self.assertEqual(self.source.read_bytes(), self.original)
