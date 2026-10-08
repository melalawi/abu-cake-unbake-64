"""Primed command reader across Git rewrites, using unchanged fixed32 stage events."""

import hashlib
import json
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from tests.project_fixture import ProjectCase
from unbake import buildfiles, config
from unbake.project import publication_push
from unbake.report import progress
from unbake.work import attempts

FIXTURE = Path(__file__).parent / "fixtures/rebase_native_data"


class RebaseLedgerReaderTests(ProjectCase):
    def setUp(self):
        super().setUp()
        raw = (FIXTURE / "events.json").read_bytes()
        provenance = json.loads((FIXTURE / "provenance.json").read_text())
        self.assertEqual(hashlib.sha256(raw).hexdigest(), provenance["events_sha256"])
        rows = json.loads(raw)
        self.events = [rows["base"], *rows["parents"], *rows["qualified"]]
        self.project = replace(self.project, id=rows["base"]["project_id"])
        self.base = b"".join(attempts.encoded(e) + b"\n" for e in self.events[:2])
        self.new = b"".join(attempts.encoded(e) + b"\n" for e in [self.events[1], *self.events[2:], self.events[0]])
        self.path = self.project.root / attempts.PATH
        self.path.write_bytes(self.base)

    def test_actual_events_larger_same_inode_git_rewrite_invalidates_primed_offset(self):
        with attempts.command_ledger(self.project) as history:
            history._refresh()
            inode, offset = history.inode, history.offset

            def git(project, *args):
                self.assertEqual(args, ("rebase", "FETCH_HEAD"))
                # Git's known rewrite boundary: same inode, changed prefix and
                # more bytes, so an old append offset lands inside an event.
                self.path.write_bytes(self.new)
                self.assertEqual((self.path.stat().st_dev, self.path.stat().st_ino), inode)
                self.assertGreater(self.path.stat().st_size, offset)
                return ""

            with patch.object(publication_push, "_git", side_effect=git):
                publication_push.rebase(self.project, self.host)
            history._refresh()
            self.assertEqual(history.events, {e["event_id"]: e for e in self.events})
            self.assertEqual(history.offset, len(self.new))

    def test_actual_marker_stages_union_before_root_read_preserves_events_and_cas(self):
        stages = (self.base, self.base, self.new)
        marker = b"<<<<<<< HEAD\n" + self.base + b"=======\n" + self.new + b">>>>>>> fixed32\n"
        with attempts.command_ledger(self.project) as history:
            history._refresh()
            original_refresh = history._refresh

            def refresh():
                self.assertNotIn(b"<<<<<<<", self.path.read_bytes())
                original_refresh()

            def git(project, *args):
                if args == ("rebase", "FETCH_HEAD"):
                    self.path.write_bytes(marker)
                    raise config.Held(config._cause("git.rebase", "conflict", owner="fixture", stage="publish"))
                if args[0] == "diff":
                    return attempts.PATH + "\0"
                if args[0] == "show":
                    return stages[int(args[-1].split(":")[1]) - 1].decode()
                if args[0] in {"add", "-c"}:
                    return ""
                raise AssertionError(args)

            with (
                patch.object(publication_push, "_git", side_effect=git),
                patch.object(history, "_refresh", side_effect=refresh),
                patch.object(buildfiles, "write_progress", return_value=[]),
                patch.object(
                    progress, "write", side_effect=AssertionError("source/proof publisher must not write union reports")
                ),
            ):
                publication_push.rebase(self.project, self.host)
            history._refresh()
            self.assertEqual(history.events, {e["event_id"]: e for e in self.events})
            self.assertEqual(
                {e["event_id"]: e for e in attempts.read_records(self.path.read_bytes(), self.project)}, history.events
            )
            self.assertTrue(attempts.storage_paths(self.project))

    def test_git_rewrite_invalidation_keeps_genuine_corruption_strict(self):
        with attempts.command_ledger(self.project) as history:
            history._refresh()

            def git(project, *args):
                self.path.write_bytes(self.base + b"not JSON\n")
                return ""

            with patch.object(publication_push, "_git", side_effect=git):
                publication_push.rebase(self.project, self.host)
            with self.assertRaises(config.Held) as held:
                history._refresh()
            self.assertEqual(held.exception.key, "ledger.corrupt")

    def test_invalid_stage_preserves_marker_working_file(self):
        self.path.write_bytes(b"<<<<<<< HEAD\ninvalid working merge\n")
        before = self.path.read_bytes()

        def git(project, *args):
            if args[0] == "diff":
                return attempts.PATH + "\0"
            if args[0] == "show":
                return "not JSON\n" if args[-1] == ":3:" + attempts.PATH else self.base.decode()
            raise AssertionError(args)

        with patch.object(publication_push, "_git", side_effect=git), self.assertRaises(ValueError):
            publication_push.resolve_conflicts(self.project, self.host)
        self.assertEqual(self.path.read_bytes(), before)
