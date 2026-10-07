"""Publication commits the unit options it proves, including unchanged dirty inputs."""

import argparse
import hashlib
import io
import subprocess
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch

import toml

from tests.project_fixture import ProjectCase
from unbake import buildfiles, config, land, runner
from unbake.cli import publish
from unbake.cli.args import Context
from unbake.compilers import drivers
from unbake.fold.apply import Folded
from unbake.layout import split
from unbake.work import attempts

FUNCTION = "func_80110CA0"
PAYLOAD = Path(__file__).parent / "fixtures/publication_options"


class PublicationOptionsTests(ProjectCase):
    def setUp(self):
        super().setUp()
        self.source = (PAYLOAD / f"{FUNCTION}.c").read_text()
        self.header = self.project.include[-1] / "span_1000/code_80110CA0.h"
        self.header.parent.mkdir(parents=True)
        self.header.write_bytes((PAYLOAD / "code_80110CA0.h").read_bytes())
        for v in self.versions:
            path = self.project.version(v).split
            path.write_text(path.read_text().replace("alpha", FUNCTION))
        path = self.project.version("us").split
        path.write_text(path.read_text().replace(f"asm, {FUNCTION}]", f"c, {FUNCTION}]"))
        self.published = self.project.src / f"{FUNCTION}.c"
        self.published.write_text(self.source + "/* previous publication */\n")
        self.config_path = self.project.root / "config.toml"
        data = toml.loads(self.config_path.read_text())
        payload = toml.loads((PAYLOAD / "config.toml").read_text())
        data["compilers"] = payload["compilers"]
        data["project"]["default_compiler"] = payload["project"]["default_compiler"]
        data["units"] = {FUNCTION: {"compiler": "ido-7.1"}}
        self.config_path.write_text(toml.dumps(data))
        (self.project.tools / "gcc-2.7.2-kmc").mkdir()
        Path(self.host.n64link).write_text("#!/bin/sh\nprintf '%s\\n' '" + buildfiles.N64LINK_RELEASE.strip() + "'\n")
        self.project = config.load(self.project.root)
        buildfiles.write(self.project, self.host)
        self.git("init", "-q")
        self.git("config", "user.name", "Fixture")
        self.git("config", "user.email", "fixture@example.test")
        self.git("add", ".")
        self.git("commit", "-qm", "Initial fixture")
        self.initial = self.git("rev-parse", "HEAD").strip()
        self.file = self.project.work / FUNCTION / f"{FUNCTION}.c"
        self.file.parent.mkdir(parents=True)
        self.file.write_text(self.source)
        self.compiled = []
        self.linked = []
        self.dependencies = []

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.project.root, check=True, capture_output=True, text=True).stdout

    def options(self, *, flags=("-O1",), default=False, regenerate=True):
        data = toml.loads(self.config_path.read_text())
        ident = data["project"]["default_compiler"] if default else "ido-7.1"
        data["units"][FUNCTION] = {"compiler": ident, "flags": list(flags)}
        self.config_path.write_text(toml.dumps(data))
        self.project = config.load(self.project.root)
        if regenerate:
            buildfiles.write(self.project, self.host)

    def run_publish(self, *, all_versions=False, mismatch=False, during_compile=None, compiler="ido-7.1"):
        attempt = attempts.Attempt(
            "t",
            FUNCTION,
            hashlib.sha256(self.file.read_bytes()).hexdigest(),
            12,
            {
                v: {"percent": 100 if v == "us" or all_versions else 50, "exact": v == "us" or all_versions}
                for v in self.versions
            },
            100 if all_versions else 50,
            all_versions,
            0.1,
            compiler,
        )
        attempts.append(self.project, attempt)

        def compile(view, host, file, version, **kwargs):
            self.compiled.append((version, view.compiler_reference(FUNCTION), drivers.flags(view, version, FUNCTION)))
            if during_compile is not None:
                during_compile()
            return nullcontext(self.root / f"{FUNCTION}.o")

        def link(project, host, obj, version, row, work, file):
            self.linked.append(version)
            return bytes(row.end - row.start) if mismatch else split.words(project, row)

        def dependencies(view, host, file, version, **kwargs):
            self.dependencies.append(version)
            return {self.header}

        args = argparse.Namespace(files=[self.file], original=[], require_version=["us"], events=True, fuzzy=False)
        stream = io.StringIO()
        with (
            patch("unbake.fold.apply.fold", return_value=Folded(FUNCTION, self.source, {}, ())),
            patch("unbake.pool.run", side_effect=lambda host, fn, jobs: [fn(job) for job in jobs]),
            patch.object(runner, "compile_unit", side_effect=compile),
            patch.object(runner, "place"),
            patch.object(runner, "link", side_effect=link),
            patch.object(runner, "dependencies", side_effect=dependencies),
            patch.object(land.steps, "record"),
            patch.object(land.steps, "ensure"),
            patch("unbake.report.progress.write", return_value=[]),
        ):
            result = publish.run(Context("publish", args, self.project.root, None, stream, self.host))
        return result

    def assert_proof_count(self, versions):
        self.assertEqual([v for v, _, _ in self.compiled], versions)
        self.assertEqual(self.linked, versions)
        self.assertEqual(self.dependencies, versions)

    def test_unchanged_dirty_config_and_generated_rules_commit_with_proved_source(self):
        self.options()
        config_bytes = self.config_path.read_bytes()
        units_bytes = (self.project.root / "units.mk").read_bytes()
        self.assertIn((PAYLOAD / "units.mk").read_bytes(), units_bytes)
        eu = self.project.version("eu").split.read_bytes()
        unrelated = self.project.root / "unrelated.txt"
        unrelated.write_text("independent staged work\n")
        self.git("add", "unrelated.txt")
        result = self.run_publish()
        self.assertEqual(result.status, "ok", result.data)
        self.assertEqual(self.git("show", "HEAD:config.toml").encode(), config_bytes)
        self.assertEqual(self.git("show", "HEAD:units.mk").encode(), units_bytes)
        self.assertEqual(self.git("show", f"HEAD:src/{FUNCTION}.c"), self.source)
        self.assertEqual(self.project.version("eu").split.read_bytes(), eu)
        self.assertEqual(result.data["versions"][FUNCTION], ["us"])
        self.assertEqual(self.git("diff", "--cached", "--name-only").strip(), "unrelated.txt")
        self.assert_proof_count(["us"])
        self.assertEqual(self.compiled[0][1], "ido-7.1")
        self.assertEqual(self.compiled[0][2][-1], "-O1")

    def test_default_identity_preserves_flags_and_stages_new_generated_rules(self):
        self.options(default=True, regenerate=False)
        result = self.run_publish(compiler="gcc-2.7.2-kmc")
        self.assertEqual(result.status, "ok", result.data)
        committed = toml.loads(self.git("show", "HEAD:config.toml"))
        self.assertEqual(committed["units"][FUNCTION], {"compiler": "gcc-2.7.2-kmc", "flags": ["-O1"]})
        self.assertIn("UNIT_CODEGEN := -O1", self.git("show", "HEAD:units.mk"))
        self.assert_proof_count(["us"])
        self.assertEqual(self.compiled[0][2][-1], "-O1")

    def test_removed_flags_are_also_committed(self):
        self.options()
        self.git("add", "config.toml", "units.mk")
        self.git("commit", "-qm", "Initial unit options")
        self.options(flags=())
        result = self.run_publish()
        self.assertEqual(result.status, "ok", result.data)
        self.assertNotIn("UNIT_CODEGEN := -O1", self.git("show", "HEAD:units.mk"))
        self.assertFalse(toml.loads(self.git("show", "HEAD:config.toml"))["units"][FUNCTION].get("flags"))
        self.assert_proof_count(["us"])

    def test_already_published_versions_still_require_proof(self):
        path = self.project.version("eu").split
        path.write_text(path.read_text().replace(f"asm, {FUNCTION}]", f"c, {FUNCTION}]"))
        self.git("add", "versions/eu/game.yaml")
        self.git("commit", "-qm", "Existing EU C")
        self.options()
        result = self.run_publish(all_versions=True)
        self.assertEqual(result.status, "ok", result.data)
        self.assertEqual(result.data["versions"][FUNCTION], ["us", "eu"])
        self.assert_proof_count(["us", "eu"])

    def test_unproved_other_unit_and_global_config_changes_refuse(self):
        self.options()
        before = toml.loads(self.config_path.read_text())
        for change in ("other_unit", "compiler", "build", "project", "version"):
            with self.subTest(change=change):
                data = toml.loads(toml.dumps(before))
                if change == "other_unit":
                    data["units"]["beta"] = {"compiler": "ido-7.1", "flags": ["-O1"]}
                elif change == "compiler":
                    data["compilers"]["ido-7.1"]["cflags"][-1] = "-O1"
                elif change == "build":
                    data["build"]["cppflags"] = ["-DUNPROVED"]
                elif change == "project":
                    data["project"]["title"] = "Unproved metadata"
                else:
                    data["version"]["eu"]["macros"] = ["VERSION_EU", "UNPROVED"]
                self.config_path.write_text(toml.dumps(data))
                self.project = config.load(self.project.root)
                source = self.published.read_bytes()
                index = (self.project.root / ".git/index").read_bytes()
                result = self.run_publish()
                self.assertEqual(result.status, "held", result.data)
                self.assertEqual(result.key, "land.config")
                self.assertEqual(self.git("rev-parse", "HEAD").strip(), self.initial)
                self.assertEqual(self.published.read_bytes(), source)
                self.assertEqual((self.project.root / ".git/index").read_bytes(), index)
                self.assertTrue(self.file.is_file())
        self.assert_proof_count(["us"] * 5)

    def test_failed_native_proof_does_not_commit_dirty_options(self):
        self.options()
        source = self.published.read_bytes()
        result = self.run_publish(mismatch=True)
        self.assertEqual(result.status, "held", result.data)
        self.assertEqual(result.key, "land.mismatch")
        self.assertIn("land.mismatch", result.data["failed"][FUNCTION]["reason"])
        self.assertEqual(self.git("rev-parse", "HEAD").strip(), self.initial)
        self.assertEqual(self.published.read_bytes(), source)
        self.assertEqual([v for v, _, _ in self.compiled], ["us"])
        self.assertEqual(self.linked, ["us"])
        self.assertEqual(self.dependencies, [])

    def test_config_changed_during_proof_refuses_before_source_write(self):
        self.options()
        source = self.published.read_bytes()

        def change():
            self.config_path.write_text(self.config_path.read_text().replace('"-O1"', '"-O2"'))

        result = self.run_publish(during_compile=change)
        self.assertEqual(result.status, "held", result.data)
        self.assertEqual(result.key, "land.config")
        self.assertEqual(self.git("rev-parse", "HEAD").strip(), self.initial)
        self.assertEqual(self.published.read_bytes(), source)
        self.assert_proof_count(["us"])

    def test_commit_refusal_restores_source_options_rows_and_previous_index(self):
        self.options()
        unrelated = self.project.root / "unrelated.txt"
        unrelated.write_text("staged user bytes\n")
        self.git("add", "unrelated.txt")
        saved = {p: p.read_bytes() for p in (self.published, self.config_path, self.project.version("us").split)}
        hook = self.project.root / ".git/hooks/pre-commit"
        hook.write_text("#!/bin/sh\nexit 1\n")
        hook.chmod(0o755)
        index = (self.project.root / ".git/index").read_bytes()
        result = self.run_publish(compiler="gcc-2.7.2-kmc")
        self.assertEqual(result.status, "held", result.data)
        self.assertEqual(self.git("rev-parse", "HEAD").strip(), self.initial)
        self.assertEqual({p: p.read_bytes() for p in saved}, saved)
        self.assertEqual((self.project.root / ".git/index").read_bytes(), index)
        self.assertTrue(self.file.is_file())
        self.assert_proof_count(["us"])
