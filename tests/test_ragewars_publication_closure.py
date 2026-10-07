"""Real route 3: refresh must keep the declarations publication could reach."""

import hashlib
import json
import os
import shutil
import subprocess
import tomllib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tests.project_fixture import ProjectCase
from unbake import config, land
from unbake.config import Held
from unbake.fold import imports
from unbake.fold.apply import Folded
from unbake.layout import apply, header_loss, index
from unbake.layout import map as ownership

FIXTURE = Path(__file__).parent / "fixtures/publication_headers/ragewars"
FUNCTION = "func_802B56E0_eu_x"
PROVIDER = "span_1000/code_802B369C.h"
UMBRELLA = "common/unused.h"
FILTER = "common/types_1dc8418c21db.h"
TYPEDEF = "typedef struct ALMainBus_s ALMainBus_s;"


class RageWarsPublicationClosureTests(ProjectCase):
    versions = ("eu-x", "us-rev1")

    def setUp(self):
        super().setUp()
        self.include = self.project.include[-1]
        shutil.copytree(FIXTURE / "after/include", self.include, dirs_exist_ok=True)
        for version in self.versions:
            for path in (self.project.version(version).split, self.project.version(version).symbols):
                path.write_text(path.read_text().replace("alpha", FUNCTION))
        self.ownership = ownership.Map(32, (ownership.Group("code_802B4730", "span_1000", "default", (FUNCTION,)),))
        (self.project.root / "layout.toml").write_bytes(ownership.encoded(self.ownership))
        self.project = config.load(self.project.root)
        self.published = (FIXTURE / "published.c").read_text()
        self.repaired = (FIXTURE / "repaired.c").read_text()
        self.options = tomllib.loads((FIXTURE / "config.toml").read_text())
        self.contents = {p: p.read_text() for p in self.include.rglob("*.h")}
        self.lookup = index.overlay(
            {"schema": 1, "headers": {}, "symbols": {}, "clusters": {}},
            {
                p.relative_to(self.include).as_posix(): t
                for p, t in self.contents.items()
                if index.marked(p, self.include)
            },
        )
        # The captured historical umbrella really supplied this name. The old
        # disposable index is unavailable: explicitly model that stale mapping.
        self.lookup["symbols"]["ALMainBus_s"] = UMBRELLA
        self.lookup["type_headers"]["ALMainBus_s"] = [UMBRELLA]
        self.compiles = []

    def rewritten(self):
        with patch.object(index, "load", return_value=self.lookup):
            resolved = imports.resolve(self.project, SimpleNamespace(texts=self.contents), self.published, FUNCTION)
            with patch.object(index, "overlay", wraps=index.overlay) as catalogue:
                source = apply.source(
                    self.project,
                    resolved,
                    FUNCTION,
                    {},
                    ownership=self.ownership,
                    previous=set(self.lookup["headers"]),
                )
        return source, catalogue

    def native(self, view, file, version):
        depfile = file.parent / f"{version}.d"
        preprocessed = subprocess.run(
            [
                "/usr/bin/cpp",
                *self.options["build"]["cppflags"],
                *(f"-D{x}" for x in self.options["version"][version]["macros"]),
                "-MMD",
                "-MF",
                str(depfile),
                *(f"-I{p}" for p in view.include),
                str(file),
            ],
            capture_output=True,
            text=True,
            cwd=self.project.root,
        )
        self.assertEqual(preprocessed.returncode, 0, preprocessed.stderr)
        compiled = subprocess.run(
            [
                "/usr/bin/gcc",
                "-m32",
                "-x",
                "c",
                "-std=c89",
                "-fsyntax-only",
                "-Werror=implicit-function-declaration",
                "-",
            ],
            input=preprocessed.stdout,
            capture_output=True,
            text=True,
        )
        self.compiles.append(version)
        if compiled.returncode:
            raise Held("compile", compiled.stderr)
        names = depfile.read_text().replace("\\\n", "").partition(":")[2].split()
        return {Path(os.path.abspath(n)) for n in names if Path(os.path.abspath(n)) != file}

    def prove_versions(self, project, host, view, function, file, versions):
        self.assertEqual(function, FUNCTION)
        return set().union(*(self.native(view, file, v) for v in versions))

    def write_source(self, text):
        file = self.project.work / FUNCTION / f"{FUNCTION}.c"
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(text)
        return file

    def git(self, *args):
        result = subprocess.run(["git", *args], cwd=self.project.root, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def publish(self, source, *, commit_error=False):
        file = self.write_source(source)
        attempt = SimpleNamespace(
            compiler=self.project.default_compiler, sha256=hashlib.sha256(file.read_bytes()).hexdigest()
        )
        receipts = []
        with (
            patch.object(land, "exact_attempt", return_value=attempt),
            patch("unbake.fold.apply.fold", return_value=Folded(FUNCTION, source, {}, ())),
            patch("unbake.fold.apply.private_headers", return_value={}),
            patch.object(land, "_prove_versions", side_effect=self.prove_versions),
            patch.object(land.buildfiles, "write", return_value=[]),
            patch("unbake.report.progress.write", return_value=[]),
            patch.object(
                land,
                "_commit",
                side_effect=Held("land", "commit failure") if commit_error else None,
                wraps=None if commit_error else land._commit,
            ),
        ):
            commit = land.land(self.project, self.host, file, on_commit=receipts.append)
        return commit, receipts

    def initialize_git(self):
        self.git("init", "-q")
        self.git("config", "user.name", "Fixture")
        self.git("config", "user.email", "fixture@example.com")
        self.git("add", "config.toml", "layout.toml", "include", "versions", "tools")
        self.git("commit", "-qm", "Baseline declaration inputs")

    def test_fixture_payload_digests_are_pinned(self):
        provenance = json.loads((FIXTURE / "provenance.json").read_text())
        for name, record in provenance["files"].items():
            self.assertEqual(hashlib.sha256((FIXTURE / name).read_bytes()).hexdigest(), record["sha256"], name)

    def test_historical_publication_passes_then_refresh_breaks_then_repair_passes_both_versions(self):
        file = self.write_source(self.published)
        umbrella = self.include / UMBRELLA
        old = (FIXTURE / "before" / UMBRELLA).read_text()
        new = umbrella.read_text()
        for version in self.versions:
            umbrella.write_text(old)
            self.native(self.project, file, version)
            umbrella.write_text(new)
            with self.assertRaisesRegex(Held, "ALMainBus_s"):
                self.native(self.project, file, version)
            file.write_text(self.repaired)
            self.native(self.project, file, version)
            file.write_text(self.published)
        self.assertEqual(self.compiles, [v for v in self.versions for _ in range(3)])

    def test_latest_provider_overlay_keeps_real_typedef_and_transitive_filter_with_stale_index(self):
        source, catalogue = self.rewritten()
        file = self.write_source(source)
        for version in self.versions:
            self.assertIn(self.include / FILTER, self.native(self.project, file, version))
        self.assertEqual(self.compiles, list(self.versions))
        self.assertIn(f'#include "{PROVIDER}"', source)
        self.assertEqual(
            source[source.index("/* alMainBusPull") :], self.published[self.published.index("/* alMainBusPull") :]
        )
        self.assertEqual(catalogue.call_count, 1)
        self.assertLessEqual(len(catalogue.call_args.args[1]), 5)
        self.assertIn(PROVIDER, catalogue.call_args.args[1])
        self.assertIn(FILTER, catalogue.call_args.args[1])

    def test_refresh_requires_source_to_reach_the_new_provider(self):
        source = self.project.src / f"{FUNCTION}.c"
        source.write_text(self.published)
        umbrella = self.include / UMBRELLA
        proposed = umbrella.read_bytes()
        umbrella.write_bytes((FIXTURE / "before" / UMBRELLA).read_bytes())
        with self.assertRaisesRegex(Held, "would lose reachable declarations.*ALMainBus_s"):
            header_loss.check(self.project, {umbrella: proposed})
        rewritten, _ = self.rewritten()
        header_loss.check(self.project, {umbrella: proposed, source: rewritten.encode()})

    def test_typedef_and_complete_struct_are_separately_required_by_real_native_proof(self):
        provider = self.include / PROVIDER
        original = provider.read_text()
        for body in (
            original.replace(TYPEDEF, ""),
            original.replace(
                "struct ALMainBus_s {\n    ALFilter_s14 filter;\n"
                "    s32 sourceCount;\n    s32 maxSources;\n"
                "    ALFilter_s14 **sources;\n};",
                "",
            ),
        ):
            provider.write_text(body)
            file = self.write_source(self.repaired)
            for version in self.versions:
                with self.assertRaisesRegex(Held, "ALMainBus_s"):
                    self.native(self.project, file, version)
        self.assertEqual(self.compiles, list(self.versions) * 2)

    def test_publication_commits_exact_source_and_native_dependencies_without_a_second_proof(self):
        source, _ = self.rewritten()
        self.initialize_git()
        original_headers = {p: p.read_bytes() for p in self.include.rglob("*.h")}
        commit, receipts = self.publish(source)
        self.assertEqual(self.git("rev-parse", "HEAD").strip(), commit)
        self.assertEqual(self.git("show", f"HEAD:src/{FUNCTION}.c"), source)
        self.assertEqual(self.compiles, list(self.versions))
        self.assertEqual(receipts[0]["proof"]["versions"], list(self.versions))
        for name in (PROVIDER, FILTER, "abi.h", "acmd.h", "audio_callbacks.h", "types.h"):
            key = f"include/{name}"
            data = self.git("show", f"HEAD:{key}").encode()
            self.assertEqual(data, original_headers[self.include / name])
            self.assertEqual(receipts[0]["proof"]["files"][key], hashlib.sha256(data).hexdigest())
        self.assertEqual({p: p.read_bytes() for p in original_headers}, original_headers)
        self.assertFalse((self.project.work / "_land" / FUNCTION).exists())

    def test_publication_refuses_broken_closure_before_writing_or_committing(self):
        self.initialize_git()
        before = self.git("rev-parse", "HEAD")
        headers = {p: p.read_bytes() for p in self.include.rglob("*.h")}
        with self.assertRaisesRegex(Held, "ALMainBus_s"):
            self.publish(self.published)
        self.assertEqual(self.git("rev-parse", "HEAD"), before)
        self.assertFalse((self.project.src / f"{FUNCTION}.c").exists())
        self.assertEqual({p: p.read_bytes() for p in headers}, headers)
        self.assertEqual(self.compiles, ["eu-x"])
        self.assertFalse((self.project.work / "_land" / FUNCTION).exists())

    def test_commit_failure_rolls_back_proved_source_and_keeps_authored_headers(self):
        source, _ = self.rewritten()
        self.initialize_git()
        before = self.git("rev-parse", "HEAD")
        headers = {p: p.read_bytes() for p in self.include.rglob("*.h")}
        config_bytes = (self.project.root / "config.toml").read_bytes()
        with self.assertRaisesRegex(Held, "commit failure"):
            self.publish(source, commit_error=True)
        self.assertEqual(self.git("rev-parse", "HEAD"), before)
        self.assertFalse((self.project.src / f"{FUNCTION}.c").exists())
        self.assertEqual((self.project.root / "config.toml").read_bytes(), config_bytes)
        self.assertEqual({p: p.read_bytes() for p in headers}, headers)
        self.assertEqual(self.compiles, list(self.versions))
