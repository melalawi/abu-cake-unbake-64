"""Public verbs end to end on the tiny fixture: init, check, compare, publish and one cycle."""

import tempfile
from pathlib import Path

from tests.integration.project import FixtureCase, run

EXACT = "int {name}(void) {{\n    return {value};\n}}\n"


class InitTests(FixtureCase):
    def test_init_creates_a_project_with_the_fixed_layout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            code, lines, stderr = self.unbake("init", "demo", "--functions-per-header", "2", cwd=Path(directory))
            self.assertEqual((code, len(lines), lines[0]["status"]), (0, 1, "ok"), stderr)
            config = (Path(directory) / "demo" / "config.toml").read_text()
            self.assertNotIn("[paths]", config)
            self.assertNotIn("[workspace]", config)
            self.assertIn("OK(init)", stderr)


class ProjectFlowTests(FixtureCase):
    def test_check_runs_make_check_on_the_generated_makefile(self) -> None:
        code, lines, stderr = self.unbake("recompute", "buildfiles")
        self.assertEqual(code, 0, stderr)
        code, lines, stderr = self.unbake("check")
        self.assertEqual((code, lines[-1]["status"]), (0, "ok"), stderr)

    def test_compare_reports_exact_in_every_version(self) -> None:
        path = self.work("alpha", EXACT.format(name="alpha", value=1))
        code, lines, stderr = self.unbake("compare", str(path))
        self.assertEqual(code, 0, stderr)
        data = lines[-1]["data"]
        self.assertEqual(data["function"], "alpha")
        self.assertTrue(data["exact"])
        self.assertEqual(set(data["versions"]), {"us", "us-rev1"})
        self.assertTrue(all(row["exact"] for row in data["versions"].values()))

    def test_compare_of_a_different_body_is_not_exact(self) -> None:
        path = self.work("alpha", EXACT.format(name="alpha", value=9))
        code, lines, stderr = self.unbake("compare", str(path))
        self.assertEqual(code, 0, stderr)
        self.assertFalse(lines[-1]["data"]["exact"])

    def test_publish_lands_one_function_with_a_local_commit(self) -> None:
        path = self.work("alpha", EXACT.format(name="alpha", value=1))
        code, lines, stderr = self.unbake("compare", str(path))
        self.assertEqual(code, 0, stderr)
        self.assertTrue(lines[-1]["data"]["exact"])
        code, lines, stderr = self.unbake("publish", str(path))
        self.assertEqual((code, lines[-1]["status"]), (0, "ok"), stderr)
        data = lines[-1]["data"]
        self.assertEqual(data["landed"], ["alpha"])
        self.assertEqual(len(data["commits"]), 1)
        self.assertTrue((self.root / "src" / "alpha.c").is_file())
        self.assertEqual(self.subjects()[0], "Match alpha")
        accepted = data["commits"][0]
        self.assertEqual(
            run(["git", "show", f"{accepted}:src/alpha.c"], self.root).stdout, (self.root / "src/alpha.c").read_text()
        )
        # Local publication leaves subsequent generated maintenance pending.
        # Transport refuses a dirty tree and checks the committed final tree.
        remote = self.base / "remote.git"
        run(["git", "init", "--bare", str(remote)], self.root)
        run(["git", "push", str(remote), "HEAD:main"], self.root)
        code, lines, stderr = self.unbake("publish", "--push", str(remote))
        self.assertEqual((code, lines[-1]["status"]), (1, "held"), stderr)
        self.assertEqual(lines[-1]["key"], "publish.push_dirty")
        run(["git", "add", "-A"], self.root)
        run(["git", "commit", "-qm", "Refresh generated files"], self.root)
        code, lines, stderr = self.unbake("publish", "--push", str(remote))
        self.assertEqual((code, lines[-1]["status"]), (0, "ok"), stderr)
        final = run(["git", "rev-parse", "HEAD"], self.root).stdout.strip()
        self.assertEqual(run(["git", "--git-dir", str(remote), "rev-parse", "main"], self.root).stdout.strip(), final)
        self.assertEqual(run(["git", "status", "--porcelain"], self.root).stdout.strip(), "")

    def test_publish_of_a_mismatch_writes_and_commits_nothing(self) -> None:
        before = run(["git", "rev-parse", "HEAD"], self.root).stdout
        path = self.work("alpha", EXACT.format(name="alpha", value=9))
        code, lines, _stderr = self.unbake("publish", str(path))
        self.assertEqual(lines[-1]["status"], "held" if code == 1 else lines[-1]["status"])
        self.assertNotEqual(code, 0)
        self.assertEqual(run(["git", "rev-parse", "HEAD"], self.root).stdout, before)
        self.assertEqual(self.subjects(), ["Fixture"])
        self.assertEqual(list(self.root.glob("build/*/*.new")), [])

    def test_cycle_lands_and_commits_a_carryover(self) -> None:
        self.work("gamma", EXACT.format(name="gamma", value=3))
        code, lines, stderr = self.unbake("cycle", "--functions", "gamma", "--stop", "all-landed")
        self.assertEqual(code, 0, stderr)
        self.assertEqual((lines[-1]["command"], lines[-1]["status"]), ("cycle", "ok"))
        events = lines[:-1]
        names = [line["event"] for line in events]
        self.assertTrue(all(line["v"] == 2 for line in events))
        self.assertEqual([line["seq"] for line in events], sorted(line["seq"] for line in events))
        for event in ("cycle.start", "fn.landed", "fn.committed", "cycle.end"):
            self.assertIn(event, names)
        self.assertLess(names.index("fn.landed"), names.index("fn.committed"))
        self.assertEqual(events[-1]["exit"], 0)
        committed = next(line for line in events if line["event"] == "fn.committed")
        self.assertEqual(committed["message"], "Match gamma")
        self.assertEqual(
            run(["git", "show", f"{committed['commit']}:src/gamma.c"], self.root).stdout,
            (self.root / "src/gamma.c").read_text(),
        )
        self.assertEqual(run(["git", "status", "--porcelain"], self.root).stdout.strip(), "")
        code, lines, stderr = self.unbake("check")
        self.assertEqual((code, lines[-1]["status"]), (0, "ok"), stderr)
