"""Reports participate in the source and generation publication transaction."""

from pathlib import Path
from typing import Any
from unittest.mock import patch

from tests.match.support import MatchFixture
from unbake.match import queue as match
from unbake.project.config import Policy, Project
from unbake.report import files, progress
from unbake.report.progress import write as write_report


class ReportPublicationTests(MatchFixture):
    def test_publication_refreshes_reports_and_rolls_back_report_write_failure(self) -> None:
        documents = {
            version: {
                "version": 2,
                "measures": {
                    "complete_code": 16,
                    "total_code": 48,
                    "complete_units": 1,
                    "total_units": 3,
                    "fuzzy_match_percent": 100.0,
                },
            }
            for version in self.versions
        }
        readme = self.root / "README.md"
        readme.write_text(
            "## Progress\n\n"
            + progress.progress(documents, {version: version + " (release)" for version in self.versions})
            + "\n\n## End\n"
        )
        first = self.root / "versions/us/report.json"
        first.write_bytes(b"old report\n")
        paths = [readme, *(self.root / "versions" / version / "report.json" for version in self.versions)]
        before = {path: path.read_bytes() if path.exists() else None for path in paths}
        self.queue("alpha")
        original_write = files.write

        def measure(project: Project, policy: Policy, version: str, *, generation: Path) -> dict[str, Any]:
            self.assertTrue((project.src / "alpha.c").is_file())
            self.assertNotEqual(generation, self.original[version])
            import fcntl

            with (self.root / "build/.lock").open("a+b") as lock, self.assertRaises(BlockingIOError):
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with (self.root / ".unbake/state/match-queue.lock").open("a+b") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertIn(", c, alpha]", project.version(version).split.read_text())
            self.assertEqual(len(self.queued()), 1)
            self.assertEqual(self.matched(), [])
            return documents[version]

        for fail in (True, False):
            with self.subTest(fail=fail):

                def write(path: Path, content: bytes, *, refuse: bool = fail) -> None:
                    if refuse and path == readme:
                        raise OSError("README write refused")
                    original_write(path, content)

                with (
                    patch.object(progress, "write", side_effect=write_report),
                    patch.object(progress, "measure", side_effect=measure),
                    patch.object(files, "write", side_effect=write),
                ):
                    receipts = match.run(self.project, self.policy)
                if fail:
                    self.assertTrue(any("README write refused" in line for line in receipts), receipts)
                    self.assert_untouched()
                    self.assertEqual(len(self.queued()), 1)
                    self.assertEqual({path: path.read_bytes() if path.exists() else None for path in paths}, before)
                else:
                    self.assertTrue(any("alpha matched" in line for line in receipts), receipts)
                    self.assertEqual(self.queued(), [])
                    self.assertEqual(len(self.matched()), 1)
                    for path in paths[1:]:
                        self.assertIn(b'"complete_units": 1', path.read_bytes())
                    self.assertIn("16 of 48", readme.read_text())
