"""Installed and rendered link implementations must agree before process creation."""

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.project.makefile_fixture import fixture, write_rendered
from unbake.match import relink
from unbake.project import build, makefile, setup
from unbake.project.config import Held


class HelperFreshnessTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(patch.stopall)

    def test_refresh_preserves_previous_recipe_provenance(self):
        with tempfile.TemporaryDirectory() as directory:
            project, _policy = fixture(Path(directory).resolve(), case=self)
            write_rendered(project)
            recipe = project.tools / "build.json"
            before = recipe.read_bytes()
            settings = {path: path.read_bytes() for path in (project.tools / "compile").rglob("*.json")}
            (project.root / "config.toml").write_text(
                (project.root / "config.toml").read_text().replace("asflags = []", 'asflags = ["--changed"]')
            )
            self.assertNotEqual(json.loads(before), makefile.description(project))
            setup.refresh_helpers(project)
            self.assertEqual(recipe.read_bytes(), before)
            self.assertEqual({path: path.read_bytes() for path in settings}, settings)
            for relative, content in makefile.driver_settings(project).items():
                self.assertEqual((project.root / relative).read_text(), content)

    def test_refresh_and_refusal_table(self):
        for state in ("current", "changed", "missing"):
            with self.subTest(state=state), patch.dict(os.environ), tempfile.TemporaryDirectory() as directory:
                project, policy = fixture(Path(directory).resolve(), case=self)
                write_rendered(project)
                helper = project.tools / "layout.py"
                if state == "changed":
                    helper.write_text("# previous installed version\n")
                elif state == "missing":
                    helper.unlink()
                if state == "current":
                    setup.require_helpers(project)
                else:
                    for boundary in ("build", "relink"):
                        with (
                            self.subTest(boundary=boundary),
                            patch.object(build, "subprocess") as build_process,
                            patch.object(relink, "subprocess") as link_process,
                        ):
                            with self.assertRaisesRegex(Held, "project helpers stale: run unbake .* setup"):
                                if boundary == "build":
                                    build.build(
                                        project,
                                        policy,
                                        ["us"],
                                        tree=project.root,
                                        generation_for=lambda _, project=project: project.build / "us.1",
                                    )
                                else:
                                    relink.prove(project, policy, "us", project.build / "us.1", None, extracted=True)
                            build_process.run.assert_not_called()
                            link_process.run.assert_not_called()
                setup.refresh_helpers(project)
                setup.require_helpers(project)
                manifest = (project.tools / "compiler.sha256").read_text()
                for relative, content in makefile.helpers(project).items():
                    self.assertEqual((project.root / relative).read_text(), content)
                    if relative.endswith(".py"):
                        self.assertIn(f"{hashlib.sha256(content.encode()).hexdigest()}  {relative}\n", manifest)
                paths = [helper, project.tools / "compiler.sha256"]
                timestamps = [p.stat().st_mtime_ns for p in paths]
                setup.refresh_helpers(project)
                self.assertEqual([p.stat().st_mtime_ns for p in paths], timestamps)

    def test_refresh_restores_every_scoped_recipe_without_rewriting_current_files(self):
        for state in ("missing", "stale"):
            with self.subTest(state=state), patch.dict(os.environ), tempfile.TemporaryDirectory() as directory:
                project, _policy = fixture(Path(directory).resolve(), case=self)
                write_rendered(project)
                expected = makefile.helpers(project)
                recipes = [name for name in expected if name.endswith(".json") and not name.endswith("build.json")]
                self.assertTrue(any("/compile/" in name for name in recipes))
                provenance = (project.tools / "build.json").read_bytes()
                for name in recipes:
                    path = project.root / name
                    if state == "missing":
                        path.unlink()
                    else:
                        path.write_text("stale")
                setup.refresh_helpers(project)
                manifest = (project.tools / "compiler.sha256").read_text()
                for name in recipes:
                    self.assertEqual((project.root / name).read_text(), expected[name])
                    self.assertIn(f"{hashlib.sha256(expected[name].encode()).hexdigest()}  {name}\n", manifest)
                self.assertEqual((project.tools / "build.json").read_bytes(), provenance)
                timestamps = {name: (project.root / name).stat().st_mtime_ns for name in expected}
                setup.refresh_helpers(project)
                self.assertEqual({name: (project.root / name).stat().st_mtime_ns for name in expected}, timestamps)

    def test_refresh_transaction_restores_files_and_pins_on_write_failure(self):
        for fail_at in (1, 3, 7):
            with self.subTest(fail_at=fail_at), patch.dict(os.environ), tempfile.TemporaryDirectory() as directory:
                project, _policy = fixture(Path(directory).resolve(), case=self)
                write_rendered(project)
                (project.tools / "layout.py").write_text("old helper")
                (project.tools / "extract.json").unlink()
                (project.tools / "link.json").write_text("old recipe")
                before = {p: p.read_bytes() for p in project.tools.rglob("*") if p.is_file()}
                original = setup.compiler_files.atomic_bytes
                count = 0

                def write(path, content, original=original, fail_at=fail_at):
                    nonlocal count
                    count += 1
                    if count == fail_at:
                        raise OSError("interrupted refresh")
                    original(path, content)

                # Make enough outputs differ to inject failures at every boundary.
                for name in makefile.helper_sources(project):
                    (project.root / name).write_text("old helper")
                before = {p: p.read_bytes() for p in project.tools.rglob("*") if p.is_file()}
                with patch.object(setup.compiler_files, "atomic_bytes", side_effect=write), self.assertRaises(OSError):
                    setup.refresh_helpers(project)
                self.assertEqual({p: p.read_bytes() for p in project.tools.rglob("*") if p.is_file()}, before)
