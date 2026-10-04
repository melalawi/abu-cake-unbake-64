"""Tracked project hygiene and generated ignore rules."""

import os
import tempfile
import unittest
from pathlib import Path

from tests.project_fixture import make
from unbake.project import hygiene


class HygieneTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(dir=os.environ["TMPDIR"])
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.project, self.policy = make(self.root)
        from unittest.mock import patch

        self.addCleanup(patch.stopall)
        from tests.git_fixture import Index
        from tests.kit import boundary

        self.index = Index(self.root)
        mock = boundary(hygiene, self.index.run)
        mock.start()
        self.addCleanup(mock.stop)

    def track(self, name: str, content: bytes) -> Path:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        self.index.add(name)
        return path

    def test_tracked_baserom_identity_remains_visible_and_ignore_is_idempotent(self) -> None:
        self.track("baserom.sha1", b"identity")
        ignore = self.root / ".gitignore"
        ignore.write_text(hygiene.ignore_text(self.project))
        self.assertIn("!baserom.sha1\n", ignore.read_text())
        self.assertEqual(hygiene.ignore_text(self.project), ignore.read_text())

    def test_tracked_version_identity_files_get_the_same_exception(self) -> None:
        self.track("versions/us/baserom.sha1", b"identity")
        ignore = self.root / ".gitignore"
        ignore.write_text(hygiene.ignore_text(self.project))
        self.assertIn("!baserom.sha1\n", ignore.read_text())
        self.assertEqual(hygiene.ignore_text(self.project), ignore.read_text())
