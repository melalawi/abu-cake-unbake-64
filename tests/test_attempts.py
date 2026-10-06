"""The committed attempt summary: merge with local logs, deterministic bytes, refusal by name, and its readers."""

import json
from unittest.mock import patch

from tests.project_fixture import ProjectCase
from unbake import land
from unbake.config import Held
from unbake.report import progress
from unbake.work import attempts
from unbake.work.attempts import Summary


def logged(project, function: str, t: str, percents: dict[str, float], exact: bool = False) -> None:
    versions = {version: {"percent": percent} for version, percent in percents.items()}
    best = max(percents.values())
    attempts.append(project, attempts.Attempt(t, function, "0" * 64, 12, versions, best, exact, 30.0, "ido-7.1"))


class MergeTests(ProjectCase):
    def test_merge(self) -> None:
        committed = Summary(12, {"us": 40.0, "eu": 70.0}, False, 9.5, 6)
        local = Summary(16, {"us": 55.5}, True, 2.0, 2)
        cases = (
            ("committed only", committed, None, committed),
            ("local only", None, local, local),
            ("both", committed, local, Summary(16, {"us": 55.5, "eu": 70.0}, True, 9.5, 6)),
            ("exact stays", Summary(12, {"us": 100.0}, True, 1.0, 1), Summary(12, {"us": 0.0}, False, 3.0, 4),
             Summary(12, {"us": 100.0}, True, 3.0, 4)),
        )  # fmt: skip
        for name, old, new, expected in cases:
            with self.subTest(name):
                self.assertEqual(attempts.merge(old, new), expected)

    def test_summaries_merge_file_and_logs_and_writing_drops_old_rows(self) -> None:
        stale = Summary(8, {"us": 10.0}, False, 1.0, 1)
        table = {"beta": Summary(12, {"us": 70.0}, False, 5.0, 3), "gone": stale}
        attempts.summary_path(self.project).write_bytes(attempts.encode(table))
        logged(self.project, "beta", "2026-10-04T10:00:00+00:00", {"us": 33.333333, "eu": 80.004})
        logged(self.project, "beta", "2026-10-04T10:01:00+00:00", {"us": 75.126, "eu": 12.0})
        read = attempts.summaries(self.project)
        self.assertEqual(read["beta"], Summary(12, {"us": 75.126, "eu": 80.004}, False, 5.0, 3))
        self.assertEqual(read["gone"], stale)
        path = attempts.write_summary(self.project, {"alpha", "beta", "gamma"})
        first = path.read_bytes()
        attempts.write_summary(self.project, {"alpha", "beta", "gamma"})
        self.assertEqual(path.read_bytes(), first)
        self.assertEqual(
            json.loads(first),
            {
                "functions": {
                    "beta": {
                        "attempts": 3,
                        "best": {"eu": 80.004, "us": 75.126},
                        "bytes": 12,
                        "exact": False,
                        "minutes": 5.0,
                    }
                },
                "v": 1,
            },
        )
        self.assertTrue(first.endswith(b"}\n"))

    def test_malformed_summary_is_refused_by_name(self) -> None:
        for name, content in (
            ("not json", b"{"),
            ("other schema", b'{"v": 2, "functions": {}}'),
            ("missing field", b'{"v": 1, "functions": {"beta": {"bytes": 4}}}'),
        ):
            with self.subTest(name):
                attempts.summary_path(self.project).write_bytes(content)
                with self.assertRaisesRegex(Held, r"attempts\.json: "):
                    attempts.summaries(self.project)


class ReaderTests(ProjectCase):
    def test_fresh_tree_measures_fuzzy_from_the_committed_summary(self) -> None:
        table = {"beta": Summary(12, {"us": 50.0}, False, 2.0, 2)}
        attempts.summary_path(self.project).write_bytes(attempts.encode(table))
        report = progress.measure(self.project, self.host, "us")
        measures = report["measures"]
        self.assertEqual(measures["matched_code"], 0)
        self.assertEqual(measures["total_code"], 36)
        self.assertEqual(measures["total_functions"], 3)
        self.assertEqual(measures["fuzzy_match_percent"], 16.666666)
        beta = next(unit for unit in report["units"] if unit["name"] == "beta")
        self.assertEqual(beta["functions"][0]["fuzzy_match_percent"], 50.0)

    def test_percentages_are_shortest_32_bit_floats(self) -> None:
        for value, expected in ((2.53478674704256, 2.5347867), (12.428734321550742, 12.428735), (100.0, 100.0)):
            with self.subTest(value):
                self.assertEqual(progress.f32(value), expected)

    def test_record_commits_only_when_an_attempt_changed_the_history(self) -> None:
        row = {"attempts": 1, "best": {"us": 50.0}}
        for label, before, after, expected in [
            ("reports moved, no attempt", {"beta": row}, {"beta": row}, None),
            ("first attempt", {}, {"beta": row}, ("c0ffee", ("beta",))),
            ("another attempt", {"beta": row}, {"beta": {**row, "attempts": 2}}, ("c0ffee", ("beta",))),
        ]:
            with self.subTest(label):
                calls: list[tuple[str, ...]] = []

                def git(project, *args, env=None, calls=calls):
                    calls.append(args)
                    if args[:2] == ("rev-parse", "--git-path"):
                        return str(self.project.root / ".git" / "index")
                    return {"rev-parse": "c0ffee\n"}.get(args[0], "")

                with (
                    patch("unbake.report.progress.write", return_value=[self.project.root / "README.md"]),
                    patch.object(attempts, "committed_documents", side_effect=[before, after]),
                    patch.object(land, "_git", side_effect=git),
                ):
                    self.assertEqual(land.record(self.project, self.host), expected)
                commits = [args for args in calls if "commit" in args]
                self.assertEqual(len(commits), 0 if expected is None else 1)
                if commits:
                    self.assertIn("Record attempts: beta", commits[0])
