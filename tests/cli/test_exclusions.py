"""Exclusion admission and next selection exercised through the public CLI."""

import json
import os
import sysconfig
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import toml

from tests.cli.support import MainCase
from tests.process_fakes import cli_process
from unbake.layout import split


class ExclusionTests(MainCase):
    def manifest(self, value: object, name: str = "unbake-exclusions.json"):
        path = self.root / name
        path.write_text(json.dumps(value))
        return path

    def test_excluded_explicit_draft_is_refused_before_generation(self):
        self.manifest({"schema": 1, "functions": ["alpha"]})
        with patch("unbake.decomp.m2c.draft") as generate:
            code, out, _ = self.run_main(self.args("draft", "alpha"))
        self.assertEqual(code, 1)
        self.assertIn("draft.excluded: alpha", out)
        generate.assert_not_called()

    def test_malformed_manifest_refused_on_both_commands(self):
        for value, reason in (
            ([], "exclusions.schema"),
            ({"schema": True, "functions": []}, "exclusions.schema"),
            ({"schema": 2, "functions": []}, "exclusions.schema"),
            ({"schema": 1, "functions": "alpha"}, "exclusions.functions"),
            ({"schema": 1, "functions": [1]}, "exclusions.functions"),
            ({"schema": 1, "functions": ["alpha", "alpha"]}, "exclusions.functions"),
            ({"schema": 1, "functions": ["absent"]}, "exclusions.function"),
        ):
            with self.subTest(value=value):
                self.manifest(value)
                for argv in (("next",), ("draft", "alpha")):
                    code, out, _ = self.run_main(self.args(*argv))
                    self.assertEqual(code, 1)
                    self.assertIn(reason, out)

    def test_missing_override_and_invalid_json_are_named(self):
        path = self.root / "missing.json"
        for argv in (("next",), ("draft", "alpha")):
            code, out, _ = self.run_main(self.args(*argv, "--exclude", str(path)))
            self.assertEqual(code, 1)
            self.assertIn("exclusions.file", out)
        path.write_text("{")
        code, out, _ = self.run_main(self.args("next", "--exclude", str(path)))
        self.assertEqual(code, 1)
        self.assertIn("exclusions.file", out)

    def test_next_skips_ranked_and_editable_excluded_work_and_carries_override(self):
        path = self.manifest({"schema": 1, "functions": ["alpha"]}, "reserve.json")
        for name in ("map/facts.json", "types/database.json"):
            artifact = self.project.build / name
            artifact.parent.mkdir(parents=True, exist_ok=True)
            artifact.write_text("{}")
        draft = self.project.drafts / "alpha"
        draft.mkdir(parents=True)
        (draft / "manifest.json").write_text(
            json.dumps(
                dict(
                    schema=1,
                    project_id=self.project.id,
                    workspace_id=self.project.workspace_id,
                    subject="alpha",
                    source="build/drafts/alpha/alpha.c",
                )
            )
        )
        (draft / "alpha.c").write_text("int alpha(void) { return 0; }")
        rows = [
            SimpleNamespace(function=n, aliases=(n,), versions=("us",), score=None, size=16, draft=None)
            for n in ("alpha", "beta")
        ]
        with (
            patch("unbake.decomp.type_context.required", return_value=("d" * 64, "")),
            patch("unbake.decomp.type_context.redrafts", return_value={"alpha": {}}),
            patch("unbake.cli.workflow.plan.actionable", return_value=rows),
        ):
            for mode in ((), ("--new",)):
                code, out, _ = self.run_main(self.args("next", *mode, "--exclude", str(path)))
                self.assertEqual(code, 0)
                self.assertIn("draft beta --exclude", out)
                self.assertNotIn("draft alpha", out)

    def test_empty_override_replaces_project_manifest(self):
        self.manifest({"schema": 1, "functions": ["alpha"]})
        path = self.manifest({"schema": 1, "functions": []}, "empty.json")
        with patch("unbake.cli.draft.owning_versions", side_effect=RuntimeError("passed exclusion")):
            code, out, _ = self.run_main(self.args("draft", "alpha", "--exclude", str(path)))
        self.assertEqual(code, 1)
        self.assertIn("passed exclusion", out)
        self.assertNotIn("draft.excluded", out)

    def test_alias_exclusion_refuses_canonical_draft(self):
        symbols = self.project.version("us").symbols
        symbols.write_text(symbols.read_text() + "\nreserved_alpha = 0x80001000;\n")
        self.manifest({"schema": 1, "functions": ["reserved_alpha"]})
        code, out, _ = self.run_main(self.args("draft", "alpha"))
        self.assertEqual(code, 1)
        self.assertIn("draft.excluded: alpha", out)

    def test_installed_cli_refuses_excluded_draft_and_malformed_next(self):
        script = Path(sysconfig.get_path("scripts")) / "unbake"
        self.assertTrue(script.is_file(), "Install unbake before running CLI tests")
        values = {key: str(value) if isinstance(value, Path) else value for key, value in asdict(self.policy).items()}
        policy = self.directory / "policy.toml"
        policy.write_text(toml.dumps(values))
        environment = dict(os.environ, UNBAKE_POLICY=str(policy), PYTHONNOUSERSITE="1")
        environment.pop("PYTHONPATH", None)
        path = self.manifest({"schema": 1, "functions": ["alpha"]})
        for operands, reason in (
            (("draft", "alpha"), "draft.excluded: alpha"),
            (("next",), "exclusions.schema"),
        ):
            if operands == ("next",):
                path.write_text('{"schema": 2, "functions": []}')
            result = cli_process(
                [str(script), *self.args(*operands)],
                cwd=self.directory,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
                timeout=15,
            )
            output = result.stdout + result.stderr
            self.assertEqual(result.returncode, 1, output)
            self.assertIn(reason, output)
            self.assertNotIn("Traceback", output)
            self.assertEqual(sum(line.startswith("Next:") for line in output.splitlines()), 1)


class ExclusionIdentityTests(MainCase):
    manifest = ExclusionTests.manifest

    def saved_identity(self):
        versions = {}
        for version in self.project.versions:
            versions[version] = {
                "functions": [
                    {"name": row.name, "start": row.start, "end": row.end, "address": row.address}
                    for row in split.functions(self.project, version)
                ]
            }
        value = dict(
            schema=1,
            project_id=self.project.id,
            workspace_id=self.project.workspace_id,
            rom_sha1={v: self.project.version(v).baserom_sha1 for v in self.project.versions},
            versions=versions,
        )
        path = self.project.build / "setup/layout.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value))
        return path, value

    def renamed_reservation(self):
        path = self.manifest({"schema": 1, "functions": ["alpha"]})
        evidence = self.saved_identity()
        for version in self.project.versions:
            cartridge = self.project.version(version)
            cartridge.split.write_text(cartridge.split.read_text().replace("alpha", "renamed"))
            cartridge.symbols.write_text(cartridge.symbols.read_text().replace("alpha", "renamed"))
        return path, evidence

    def test_next_reads_reserved_name_without_changing_manifest(self):
        path, _ = self.renamed_reservation()
        code, out, error = self.run_main(self.args("next"))
        self.assertEqual(code, 0, out + error)
        self.assertEqual(json.loads(path.read_bytes()), {"schema": 1, "functions": ["alpha"]})
        self.assertIn("map", out)
        before = path.stat().st_mtime_ns
        code, _, _ = self.run_main(self.args("next"))
        self.assertEqual(code, 0)
        self.assertEqual(path.stat().st_mtime_ns, before)

    def test_draft_uses_refreshed_reservation_before_generation(self):
        path, _ = self.renamed_reservation()
        with patch("unbake.decomp.m2c.draft") as generate:
            code, out, _ = self.run_main(self.args("draft", "renamed"))
        self.assertEqual(code, 1)
        self.assertIn("draft.excluded: renamed", out)
        generate.assert_not_called()
        self.assertEqual(json.loads(path.read_bytes())["functions"], ["alpha"])

    def test_changed_identity_boundary_and_unknown_names_never_refresh(self):
        path, (evidence_path, value) = self.renamed_reservation()
        before = path.read_bytes()
        for key in ("project_id", "workspace_id", "rom_sha1", "boundary", "duplicate"):
            altered = json.loads(json.dumps(value))
            if key in ("project_id", "workspace_id"):
                altered[key] = "changed"
            elif key == "rom_sha1":
                altered[key]["us"] = "0" * 40
            elif key == "boundary":
                altered["versions"]["us"]["functions"][0]["address"] += 4
            else:
                altered["versions"]["us"]["functions"].append(altered["versions"]["us"]["functions"][0])
            evidence_path.write_text(json.dumps(altered))
            code, out, _ = self.run_main(self.args("next"))
            self.assertEqual(code, 1, key)
            self.assertIn("exclusions.function", out)
            self.assertEqual(path.read_bytes(), before)
        evidence_path.write_text(json.dumps(value))
        path.write_text('{"schema": 1, "functions": ["alpha", "typo"]}')
        before = path.read_bytes()
        code, out, _ = self.run_main(self.args("next"))
        self.assertEqual(code, 1)
        self.assertIn("exclusions.function", out)
        self.assertEqual(path.read_bytes(), before)

    def test_explicit_manifest_requires_current_names(self):
        path, _ = self.renamed_reservation()
        before = path.read_bytes()
        code, out, _ = self.run_main(self.args("next", "--exclude", str(path)))
        self.assertEqual(code, 1)
        self.assertIn("exclusions.function", out)
        self.assertEqual(path.read_bytes(), before)

    def test_publication_folds_the_resolved_name_and_drops_published_reservations(self):
        from unbake.decomp import exclusions

        path, _ = self.renamed_reservation()
        before = path.read_text()
        edits = exclusions.publication_edit(self.project, {"renamed"})
        self.assertEqual(path.read_text(), before)
        self.assertEqual(len(edits), 1)
        self.assertEqual(json.loads(edits[0].after)["functions"], [])
