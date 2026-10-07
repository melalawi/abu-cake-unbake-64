"""Real Git rebase unions independent event sets and regenerates owned projections."""

import hashlib
import json
import subprocess
from unittest.mock import patch

from tests.ledger_fixture import history_bytes
from tests.project_fixture import ProjectCase
from unbake import buildfiles, config
from unbake.project import publication_push
from unbake.report import progress, verify
from unbake.work import attempts


class ConcurrentLedgerReportsTests(ProjectCase):
    def git(self, *args, check=True):
        return subprocess.run(["git", *args], cwd=self.project.root, capture_output=True, text=True, check=check)

    def report(self):
        with patch.object(progress, "readme_descriptions", return_value={v: f"{v} (fixture)" for v in self.versions}):
            return progress.write(self.project, self.host)

    def setUp(self):
        super().setUp()
        self.git("init", "-q")
        self.git("config", "user.name", "Fixture")
        self.git("config", "user.email", "fixture@example.test")
        (self.project.root / "README.md").write_text(
            "# Fixture\n\nAuthored intro.\n\n## Progress\n\n\n## Files\n\nAuthored suffix.\n"
        )
        self.project.root.joinpath(attempts.PATH).write_bytes(b"")
        buildfiles.write_progress(self.project, publish_branch=self.host.publish_branch)
        self.report()
        self.git("add", ".")
        self.git("commit", "-qm", "Initial fixture")
        self.base = self.git("rev-parse", "HEAD").stdout.strip()

    def branch(self, name, function, score):
        self.git("checkout", "-qb", name, self.base)
        source = self.project.src / (function + ".c")
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(attempts.guarded(f"int {function}(void) {{return 1;}}\n"))
        receipt = {
            "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            "compiler": self.project.compiler_reference(function),
            "score": score,
            "versions": {v: score for v in self.versions},
        }
        summary = attempts.Summary(12, {v: score for v in self.versions}, False, 1, 1, receipt)
        self.project.root.joinpath(attempts.PATH).write_bytes(history_bytes(self.project, {function: summary}))
        buildfiles.write_progress(self.project, publish_branch=self.host.publish_branch)
        self.report()
        self.git("add", ".")
        self.git("commit", "-qm", "Fixture branch " + function)
        return {r["event_id"] for r in attempts.Ledger(self.project).events.values()} or set(
            json.loads(line)["event_id"] for line in self.project.root.joinpath(attempts.PATH).read_bytes().splitlines()
        )

    def test_two_different_function_branches_union_both_histories_and_reports_without_native_proof(self):
        first = self.branch("first", "alpha", 35.0)
        second = self.branch("second", "beta", 52.0)
        failed = self.git("rebase", "first", check=False)
        self.assertNotEqual(failed.returncode, 0)
        with patch.object(publication_push.pool, "run", side_effect=AssertionError("no repeated native proof")) as jobs:
            self.assertTrue(publication_push.resolve_conflicts(self.project, self.host))
            continued = self.git("-c", "core.editor=true", "rebase", "--continue")
        self.assertEqual((continued.returncode, jobs.call_count), (0, 0))
        history = attempts.Ledger(self.project)
        summaries = history.summaries()
        self.assertEqual(set(history.events), first | second)
        self.assertEqual({k: s.attempts for k, s in summaries.items()}, {"alpha": 1, "beta": 1})
        self.assertEqual(set(history.fuzzy_sources()), {"alpha", "beta"})
        self.assertEqual(verify.validate(config.load(self.project.root))["schema"], 1)
        text = self.project.root.joinpath("README.md").read_text()
        self.assertIn("Authored intro.", text)
        self.assertIn("Authored suffix.", text)
        self.assertEqual(self.git("status", "--porcelain", "--untracked-files=no").stdout, "")

    def test_legacy_generated_ci_and_verifier_conflicts_regenerate_from_the_current_owner(self):
        payload = verify.bundle()
        with patch.object(verify, "bundle", return_value=payload + b"first"):
            first = self.branch("first", "alpha", 35.0)
        with patch.object(verify, "bundle", return_value=payload + b"second"):
            second = self.branch("second", "beta", 52.0)
        self.assertNotEqual(self.git("rebase", "first", check=False).returncode, 0)
        conflicts = set(self.git("diff", "--name-only", "--diff-filter=U").stdout.splitlines())
        self.assertTrue(
            {attempts.PATH, verify.MANIFEST, verify.BUNDLE, ".github/workflows/progress.yml", ".gitlab-ci.yml"}
            <= conflicts
        )
        with patch.object(publication_push.pool, "run", side_effect=AssertionError("no repeated native proof")):
            self.assertTrue(publication_push.resolve_conflicts(self.project, self.host))
            self.git("-c", "core.editor=true", "rebase", "--continue")
        history = attempts.Ledger(self.project)
        self.assertEqual(set(history.summaries()), {"alpha", "beta"})
        self.assertEqual(set(history.events), first | second)
        self.assertEqual(self.project.root.joinpath(verify.BUNDLE).read_bytes(), payload)
        self.assertEqual(verify.validate(config.load(self.project.root))["schema"], 1)
        self.assertEqual(self.git("status", "--porcelain", "--untracked-files=no").stdout, "")

    def test_authored_source_conflict_is_not_silently_resolved(self):
        source = self.project.src / "alpha.c"
        source.write_text("int alpha(void) {return 0;}\n")
        self.git("add", "src/alpha.c")
        self.git("commit", "-qm", "Base authored source")
        base = self.git("rev-parse", "HEAD").stdout.strip()
        self.git("checkout", "-qb", "left", base)
        source.write_text("int alpha(void) {return 1;}\n")
        self.git("add", "src/alpha.c")
        self.git("commit", "-qm", "Left authored source")
        self.git("checkout", "-qb", "right", base)
        source.write_text("int alpha(void) {return 2;}\n")
        self.git("add", "src/alpha.c")
        self.git("commit", "-qm", "Right authored source")
        self.assertNotEqual(self.git("rebase", "left", check=False).returncode, 0)
        with patch.object(publication_push.pool, "run") as jobs:
            self.assertFalse(publication_push.resolve_conflicts(self.project, self.host))
        jobs.assert_not_called()

    def test_public_push_portability_gate_runs_before_inventory_or_git_push(self):
        row = history_bytes(self.project, {"alpha": attempts.Summary(12, {}, False, 0, 1)})
        event = json.loads(row)
        event["dependencies"]["values"]["search"] = (
            "Search(quote_roots=(), include_roots=(PosixPath('" + str(self.project.include[0]) + "'),), "
            "system_roots=(), forced=(), macros=(), recipe='graph-1')"
        )
        self.project.root.joinpath(attempts.PATH).write_bytes(attempts.encoded(event) + b"\n")
        with (
            patch.object(publication_push, "_git", return_value="") as git,
            patch("unbake.report.state.inventory") as scan,
            self.assertRaises(config.Held) as caught,
        ):
            publication_push.push(self.project, self.host, "origin")
        self.assertEqual(caught.exception.key, "ledger.portability")
        self.assertEqual(scan.call_count, 0)
        self.assertFalse(any("push" in call.args for call in git.call_args_list))
