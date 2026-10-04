"""Command scratch disappears on every exit and live leases resist collection."""

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from unbake.match import publication
from unbake import config
from unbake.project import workspace


class WorkspaceTests(unittest.TestCase):
    def test_success_refusal_and_exception_remove_leased_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = SimpleNamespace(build=root)
            for failure in (None, config.Held("submit", "refused"), RuntimeError("failed")):
                with self.subTest(failure=failure):
                    try:
                        with workspace.temporary(project, prefix="submit-", directory=root) as name:
                            path = Path(name)
                            self.assertTrue((path / ".inuse").is_file())
                            (path / "artifact").write_text("scratch")
                            if failure:
                                raise failure
                    except (config.Held, RuntimeError):
                        pass
                    self.assertFalse(path.exists())

    def test_collect_keeps_live_workspace_then_removes_dead_workspaces(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = SimpleNamespace(build=root, versions=["us"])
            current = root / "us.1"
            current.mkdir()
            abandoned = [root / "submit-old", root / "setup/proof-old", root / "setup/symbol-proof-old"]
            for path in abandoned:
                path.mkdir(parents=True)
                (path / "bytes").write_text("scratch")
            durable = root / "drafts/authored.c"
            durable.parent.mkdir()
            durable.write_text("int authored;")
            with (
                patch.object(publication.build, "current_generation", return_value=current),
                workspace.temporary(project, prefix="symbol-proof-", directory=root / "setup") as active,
            ):
                publication.collect(project)
                self.assertTrue(Path(active).is_dir())
                self.assertTrue(current.is_dir())
                self.assertEqual(durable.read_text(), "int authored;")
                self.assertTrue(all(not path.exists() for path in abandoned))
            self.assertFalse(Path(active).exists())

    def test_collect_removes_unread_reviews_without_loading_large_json(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            directory = root / "setup"
            directory.mkdir()
            # Invalid review bytes are still disposable: no tool reads them.
            for name in ("symbol-proposal.json", "join-proposal.json"):
                (directory / name).write_bytes(b"unused review")
            evidence = directory / "symbol-correspondence.json"
            evidence.write_bytes(b"published evidence")
            proposal = directory / "proposal.json"
            proposal.write_bytes(b"compiler review")
            confirmation = directory / "confirmation.json"
            confirmation.write_text(json.dumps({"proposal_sha256": hashlib.sha256(proposal.read_bytes()).hexdigest()}))
            with patch.object(publication.json, "load", side_effect=AssertionError("large JSON load")):
                publication._accepted_proposals(directory)
            self.assertFalse(proposal.exists())
            self.assertFalse((directory / "symbol-proposal.json").exists())
            self.assertFalse((directory / "join-proposal.json").exists())
            self.assertEqual(evidence.read_bytes(), b"published evidence")
            self.assertTrue(confirmation.exists())

    def test_collect_keeps_unaccepted_compiler_choices_and_live_reviews(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = SimpleNamespace(build=root, versions=[])
            directory = root / "setup"
            with workspace.temporary(project, prefix="proof-", directory=directory):
                proposal = directory / "proposal.json"
                proposal.write_bytes(b"compiler choices")
                review = directory / "symbol-proposal.json"
                review.write_bytes(b"review")
                publication.collect(project)
                self.assertTrue(review.exists())
            publication.collect(project)
            self.assertTrue(proposal.exists())
            self.assertFalse(review.exists())
