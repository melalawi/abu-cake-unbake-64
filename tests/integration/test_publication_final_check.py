"""The public publisher checks the exact commit sent to a real Git remote."""

import hashlib
import io
import json
import subprocess
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

import toml

from tests.ledger_fixture import history_bytes
from tests.project_fixture import ProjectCase
from unbake import build, steps
from unbake.cli.main import main
from unbake.project import hygiene, publication_push
from unbake.work import attempts


class FinalCommitCheckTests(ProjectCase):
    def git(self, *args, cwd=None):
        return subprocess.run(
            ["git", *args], cwd=cwd or self.project.root, check=True, capture_output=True, text=True
        ).stdout.strip()

    def setUp(self):
        super().setUp()
        self.host.values["resources"]["domain"] = "standalone"
        self.config = self.root / "host.toml"
        self.config.write_text(toml.dumps(self.host.values))
        self.source = self.project.src / "alpha.c"
        self.write_source("int alpha(void) {return 1;}\n")
        (self.project.root / ".gitignore").write_text(hygiene.base_ignore_text(self.project.root))
        self.git("init", "-qb", "main")
        self.git("config", "user.name", "Fixture")
        self.git("config", "user.email", "fixture@example.test")
        self.git("add", ".")
        self.git("commit", "-qm", "Initial fixture")
        self.remote = self.root / "remote.git"
        self.git("init", "--bare", "-q", str(self.remote))
        self.git("remote", "add", "origin", str(self.remote))
        self.git("push", "-q", "origin", "main")
        self.base = self.git("rev-parse", "HEAD")
        (self.project.root / "README.md").write_text("# Fixture\n")
        self.git("add", "README.md")
        self.git("commit", "-qm", "Local authored document")
        self.head = self.git("rev-parse", "HEAD")

    def write_source(self, content):
        self.source.write_text(attempts.guarded(content))
        receipt = {
            "source_sha256": hashlib.sha256(self.source.read_bytes()).hexdigest(),
            "compiler": self.project.compiler_reference("alpha"),
            "score": 35.0,
            "versions": {version: 35.0 for version in self.project.versions},
        }
        summary = attempts.Summary(12, {}, False, 0, 1, receipt)
        (self.project.root / attempts.PATH).write_bytes(history_bytes(self.project, {"alpha": summary}))

    def public(self, after=None):
        checked = []
        original = build.check

        def basic(project, host, *, files_only):
            self.assertFalse(files_only)
            checked.append(self.git("rev-parse", "HEAD"))
            result = original(project, host, files_only=files_only)
            if after is not None:
                after(len(checked))
            return result

        out, err = io.StringIO(), io.StringIO()
        with (
            patch.object(build, "check", side_effect=basic),
            patch.object(steps, "ensure"),
            patch.object(hygiene, "tracked_findings", return_value=[]),
            patch.object(publication_push, "snapshot", return_value={}),
            redirect_stdout(out),
            redirect_stderr(err),
        ):
            code = main(
                ["--config", str(self.config), "--project", str(self.project.root), "publish", "--push", "origin"]
            )
        self.assertEqual(len(out.getvalue().splitlines()), 1)
        return code, json.loads(out.getvalue()), checked

    def remote_head(self):
        return self.git("--git-dir=" + str(self.remote), "rev-parse", "refs/heads/main")

    def test_success_checks_and_pushes_the_exact_final_sha_without_writing_admission_history(self):
        before = (self.project.root / attempts.PATH).read_bytes()
        for _ in range(2):
            code, result, checked = self.public()
            self.assertEqual((code, checked), (0, [self.head]))
            self.assertEqual(result["data"]["push"]["head"], self.head)
            self.assertEqual(self.remote_head(), self.head)
            self.assertTrue(result["data"]["push"]["check"]["built"])
            self.assertEqual((self.project.root / attempts.PATH).read_bytes(), before)
            self.assertEqual(self.git("status", "--porcelain", "--untracked-files=no"), "")

    def test_real_source_rule_failure_retains_typed_cause_and_never_pushes(self):
        self.write_source('int alpha(void) {__asm__("nop"); return 1;}\n')
        self.git("add", "src/alpha.c", attempts.PATH)
        self.git("commit", "-qm", "Source rule fixture")
        code, result, checked = self.public()
        self.assertEqual((code, len(checked)), (1, 1))
        self.assertEqual(result["key"], "check.source_rules")
        self.assertFalse(result["data"]["check"]["built"])
        self.assertTrue(result["data"]["fault"]["cause"]["dependency_set"]["files"])
        self.assertEqual(self.remote_head(), self.base)

    def test_real_basic_make_failure_is_not_a_successful_files_only_check(self):
        self.host.make.write_text("#!/bin/sh\nexit 1\n")
        code, result, checked = self.public()
        self.assertEqual((code, len(checked)), (1, 1))
        self.assertFalse(result["data"]["check"]["ok"])
        self.assertTrue(result["data"]["check"]["built"])
        self.assertEqual(self.remote_head(), self.base)

    def test_check_induced_tracked_source_change_is_preserved_and_not_pushed(self):
        code, result, _ = self.public(lambda _: self.source.write_text("int alpha(void) {return 2;}\n"))
        self.assertEqual((code, result["key"]), (1, "publish.check_changed"))
        self.assertEqual(self.source.read_text(), "int alpha(void) {return 2;}\n")
        self.assertEqual(self.remote_head(), self.base)

    def test_check_induced_commit_is_preserved_and_not_pushed(self):
        code, result, _ = self.public(lambda _: self.git("commit", "--allow-empty", "-qm", "Unexpected check commit"))
        self.assertEqual((code, result["key"]), (1, "publish.check_changed"))
        self.assertNotEqual(self.git("rev-parse", "HEAD"), self.head)
        self.assertEqual(self.remote_head(), self.base)

    def test_check_induced_untracked_output_is_not_pushed(self):
        code, result, _ = self.public(lambda _: (self.project.root / "unexpected.txt").write_text("output"))
        self.assertEqual((code, result["key"]), (1, "publish.check_changed"))
        self.assertEqual(self.remote_head(), self.base)

    def test_real_remote_race_rebases_then_checks_the_new_commit_before_pushing(self):
        other = self.root / "other"
        self.git("clone", "-qb", "main", str(self.remote), str(other))
        self.git("config", "user.name", "Fixture", cwd=other)
        self.git("config", "user.email", "fixture@example.test", cwd=other)

        def advance(count):
            if count == 1:
                (other / "upstream.txt").write_text("Remote advance\n")
                self.git("add", "upstream.txt", cwd=other)
                self.git("commit", "-qm", "Concurrent remote update", cwd=other)
                self.git("push", "-q", "origin", "main", cwd=other)

        code, result, checked = self.public(advance)
        self.assertEqual((code, len(checked)), (0, 2))
        self.assertNotEqual(checked[0], checked[1])
        self.assertEqual(result["data"]["push"]["head"], checked[-1])
        self.assertEqual(self.remote_head(), checked[-1])
