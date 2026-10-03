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
            (project.root / "config.toml").write_text(
                (project.root / "config.toml").read_text().replace("asflags = []", 'asflags = ["--changed"]')
            )
            self.assertNotEqual(json.loads(before), makefile.description(project))
            setup.refresh_helpers(project)
            self.assertEqual(recipe.read_bytes(), before)

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
