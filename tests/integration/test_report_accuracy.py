"""Current-source report semantics on small real split/source/receipt payloads."""

import hashlib
import io
import json
import os
import re
import subprocess
import sys
import zipfile
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from pathlib import Path
from typing import ClassVar
from unittest import skipUnless
from unittest.mock import patch

import toml

from tests.kit import host_values
from tests.ledger_fixture import history_bytes
from tests.project_fixture import ProjectCase
from unbake import buildfiles, config
from unbake.config import Held
from unbake.layout import split
from unbake.report import progress
from unbake.work import attempts


class ReportAccuracyTests(ProjectCase):
    def exact(self, name="alpha", versions=None):
        (self.project.src / f"{name}.c").write_text(f"int {name}(void) {{ return 1; }}\n")
        for version in versions or self.versions:
            path = self.project.version(version).split
            path.write_text(path.read_text().replace(f"asm, {name}", f"c, {name}"))

    def retained(self, name="beta", score=15.0, best=100.0):
        source = self.project.src / f"{name}.c"
        source.write_text(attempts.guarded(f"int {name}(void) {{ return 2; }}\n"))
        receipt = {
            "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            "compiler": self.project.compiler_reference(name),
            "score": score,
            "versions": {v: score for v in self.versions},
        }
        table = attempts.ledger(self.project).summaries()
        table[name] = attempts.Summary(12, {v: best for v in self.versions}, False, 1, 1, receipt)
        (self.project.root / attempts.PATH).write_bytes(history_bytes(self.project, table))
        return receipt

    def reports(self):
        from unbake.report import state

        inventory = state.inventory(self.project)
        return {v: progress.measure(self.project, self.host, v, current=inventory) for v in self.versions}

    def save(self):

        buildfiles.write_progress(self.project, publish_branch=self.host.publish_branch)
        descriptions = {v: f"{v} (fixture)" for v in self.versions}
        reports = self.reports()
        with patch.object(progress, "readme_descriptions", return_value=descriptions):
            progress.write(self.project, self.host, reports=reports)
        return reports

    def test_definition_inventory_treats_conditional_call_bodies_as_opaque(self):
        from unbake import cdecl

        source = """int choice(void) {
#if defined(VERSION_A)
            send(table[index],
#else
            send(table,
#endif
                 0);
            return 1;
        }
        """
        self.assertEqual(cdecl.declarations(source).functions, {"choice"})

    def test_history_without_source_at_zero_57_100_gets_no_fuzzy_credit(self):
        for best in (0, 57, 100):
            with self.subTest(best=best):
                table = {"beta": attempts.Summary(12, {v: best for v in self.versions}, False, 1, 1)}
                (self.project.root / attempts.PATH).write_bytes(history_bytes(self.project, table))
                report = progress.measure(self.project, self.host, "us")
                self.assertEqual(report["measures"]["fuzzy_match_percent"], 0)
                self.assertNotIn("fuzzy_match_percent", report["units"][1]["functions"][0])
                self.assertNotIn("source_path", report["units"][1]["metadata"])

    def test_low_and_unknown_drafts_use_current_receipt_not_best(self):
        from unbake.report import state, verify

        self.exact()
        self.retained(score=15, best=100)
        self.retained("gamma", score=None, best=95)
        inventory = state.inventory(self.project)
        report = progress.measure(self.project, self.host, "us", current=inventory)
        self.assertEqual(report["measures"]["matched_code"], 12)
        self.assertEqual(report["units"][1]["functions"][0]["fuzzy_match_percent"], 15)
        self.assertNotIn("fuzzy_match_percent", report["units"][2]["functions"][0])
        self.assertAlmostEqual(report["measures"]["fuzzy_match_percent"], 100 * 13.8 / 36, places=5)
        (self.project.root / verify.BUNDLE).write_bytes(b"pinned")
        values = verify.document(self.project, inventory, {"us": report})["versions"]["us"]
        self.assertEqual((values["draft_bytes"], values["draft_functions"]), (24, 2))
        self.assertEqual(values["unknown_similarity_bytes"], 12)
        self.assertEqual(values["exact_plus_draft_percent"], 100)
        self.assertIsNone(values["drafts"][1]["score"])

    def test_merged_entries_aliases_version_members_and_splits_preserve_totals(self):
        self.exact()
        source = self.project.src / "alpha.c"
        source.write_text(source.read_text() + "int inner(void) { return 2; }\n")
        for version in self.versions:
            meta = self.project.version(version)
            # Fold beta into alpha. The real symbol entry expands it again for reports.
            meta.split.write_text(meta.split.read_text().replace("      - [0x4C, asm, beta]\n", ""))
            address = 0x8000100C if version == "us" else 0x8020200C
            meta.symbols.write_text(
                meta.symbols.read_text()
                + f"inner = 0x{address:X}; // type:func\n"
                + f"inner_alias = 0x{address:X}; // type:func\n"
            )
        merged = self.reports()
        self.assertEqual(merged["us"]["measures"]["total_functions"], 3)
        self.assertEqual(merged["us"]["measures"]["matched_functions"], 2)
        self.assertEqual([int(row["size"]) for row in merged["us"]["units"][0]["functions"]], [12, 12])
        self.assertEqual(merged["us"]["measures"]["total_code"], 36)
        # Unfold the same entry using the owning split boundary.
        for version in self.versions:
            path = self.project.version(version).split
            path.write_text(
                path.read_text().replace(
                    "      - [0x58, asm, gamma]", "      - [0x4C, c, inner]\n      - [0x58, asm, gamma]"
                )
            )
        source.write_text("int alpha(void) { return 1; }\n")
        (self.project.src / "inner.c").write_text("int inner(void) { return 2; }\n")
        unfolded = self.reports()
        for version in self.versions:
            for key in ("total_code", "matched_code", "total_functions", "matched_functions"):
                self.assertEqual(merged[version]["measures"][key], unfolded[version]["measures"][key])
        # One version changes ownership without changing the other version's census.
        path = self.project.version("eu").split
        path.write_text(path.read_text().replace("c, inner", "asm, inner"))
        current = self.reports()
        self.assertEqual(current["eu"]["measures"]["matched_functions"], 1)
        self.assertEqual(current["us"]["measures"]["matched_functions"], 2)

    def test_startup_original_asm_and_retyped_data_have_honest_scope(self):
        self.exact()
        for version in self.versions:
            path = self.project.version(version).split
            path.write_text(
                path.read_text()
                .replace("asm, beta", "hasm, beta")
                .replace("      - [0x58, asm, gamma]", "      - [0x54, data, constants]\n      - [0x58, asm, gamma]")
            )
        report = progress.measure(self.project, self.host, "us")
        measures = report["measures"]
        self.assertEqual((measures["total_code"], measures["matched_code"], measures["total_data"]), (32, 12, 4))
        self.assertEqual((measures["total_functions"], measures["matched_functions"]), (3, 1))
        self.assertEqual(measures["matched_data"], 0)
        self.assertEqual(measures["matched_data_percent"], 0.0)
        categories = {row["id"]: row["measures"] for row in report["categories"]}
        self.assertEqual(categories["original_asm"]["total_code"], 8)
        self.assertEqual(categories["original_asm"]["matched_code"], 0)
        self.assertEqual(categories["data"]["total_data"], 4)
        self.assertEqual(sum(row["total_code"] for row in categories.values()), 32)

    def test_current_inventory_reads_and_parses_each_source_once_and_each_version_once(self):
        from unbake import cdecl
        from unbake.report import state

        self.exact()
        self.retained()
        original = Path.read_bytes
        reads = []

        def read(path):
            if path.suffix == ".c":
                reads.append(path)
            return original(path)

        with (
            patch.object(Path, "read_bytes", read),
            patch.object(split, "functions", wraps=split.functions) as scans,
            patch.object(cdecl, "declarations", wraps=cdecl.declarations) as parses,
            patch.object(
                attempts.Ledger,
                "fuzzy_sources",
                autospec=True,
                side_effect=lambda ledger: __import__(
                    "tests.ledger_fixture", fromlist=["current_receipts"]
                ).current_receipts(ledger),
            ) as receipts,
            patch.object(subprocess, "run") as processes,
        ):
            inventory = state.inventory(self.project)
            for version in self.versions:
                progress.measure(self.project, self.host, version, current=inventory)
        self.assertEqual(scans.call_count, 2)
        self.assertEqual(parses.call_count, 2)
        self.assertEqual(receipts.call_count, 1)
        self.assertEqual(reads, [self.project.src / "alpha.c", self.project.src / "beta.c"])
        processes.assert_not_called()

    def test_source_missing_receipt_and_hash_compiler_version_mismatch_fail(self):
        from unbake.report import state

        receipt = self.retained()
        for key, value, reason in (
            ("source_sha256", "0" * 64, "source.hash"),
            ("compiler", "other", "source.compiler"),
            ("versions", {"us": 15}, "source.versions"),
        ):
            with self.subTest(key=key):
                table = attempts.ledger(self.project).summaries()
                table["beta"] = replace(table["beta"], fuzzy={**receipt, key: value})
                (self.project.root / attempts.PATH).write_bytes(history_bytes(self.project, table))
                with self.assertRaisesRegex(Held, reason):
                    state.inventory(self.project)
        table["beta"] = replace(table["beta"], fuzzy=None)
        (self.project.root / attempts.PATH).write_bytes(history_bytes(self.project, table))
        source = (self.project.src / "beta.c").read_bytes()
        with self.assertRaisesRegex(Held, "source.receipt.*reconcile"):
            progress.measure(self.project, self.host, "us")
        self.assertEqual((self.project.src / "beta.c").read_bytes(), source)

    def test_duplicate_key_loss_sequence_refused_before_any_write(self):
        self.retained()
        original = json.loads((self.project.root / attempts.PATH).read_bytes().splitlines()[-1])
        content = attempts.encoded(original).replace(b'"schema":2', b'"schema":2,"schema":2') + b"\n"
        target = self.project.root / attempts.PATH
        target.write_bytes(content)
        self.assertEqual(target.read_bytes(), content)
        with (
            patch.object(progress, "readme_descriptions", return_value={v: f"{v} (fixture)" for v in self.versions}),
            patch("unbake.report.files.write") as writes,
            self.assertRaisesRegex(Held, "duplicate JSON key"),
        ):
            progress.write(self.project, self.host)
        writes.assert_not_called()

    def test_nested_duplicates_are_named_by_shared_input_boundary(self):
        from unbake import strict_json

        with self.assertRaisesRegex(ValueError, r"nested.json.*duplicate JSON key 'score'.*\$.fuzzy"):
            strict_json.loads('{"fuzzy":{"score":15,"score":100}}', "nested.json")

    def test_deterministic_json_readme_manifest_and_rom_free_artifact_consumer(self):
        from unbake.report import verify

        self.exact()
        self.retained()
        reports = self.save()
        paths = [
            self.project.root / "README.md",
            self.project.root / verify.MANIFEST,
            *(self.project.root / "versions" / v / "report.json" for v in self.versions),
        ]
        saved = {path: path.read_bytes() for path in paths}
        self.save()
        self.assertEqual(saved, {path: path.read_bytes() for path in paths})
        self.assertEqual(verify.bundle(), verify.bundle())
        self.assertEqual(progress._aggregate(reports)["measures"]["total_functions"], 6)
        for version in self.versions:
            self.project.version(version).baserom.unlink()
        out = self.project.root / "out"
        with (
            patch.dict(os.environ, {"GITHUB_SHA": "1" * 40}),
            patch.object(sys, "argv", ["verify", "--project", str(self.project.root), "--artifacts", str(out)]),
        ):
            self.assertEqual(verify.main(), 0)
        for version in self.versions:
            artifact = out / f"{version}_report.json"
            artifact.write_bytes(saved[self.project.root / "versions" / version / "report.json"])
            manifest = json.loads((out / f"{version}_report.manifest.json").read_bytes())
            self.assertEqual(manifest["source_commit"], "1" * 40)
            self.assertEqual(manifest["report_sha256"], hashlib.sha256(artifact.read_bytes()).hexdigest())
            self.assertEqual(
                manifest["tool_sha256"], hashlib.sha256((self.project.root / verify.BUNDLE).read_bytes()).hexdigest()
            )
            self.assertEqual(json.loads(artifact.read_bytes()), reports[version])
        # Equal totals cannot conceal a renamed entry or source-path change.
        path = self.project.root / "versions/us/report.json"
        report = json.loads(path.read_bytes())
        report["units"][0]["functions"][0]["name"] = "renamed"
        path.write_text(json.dumps(report, indent=2) + "\n")
        with self.assertRaisesRegex(Held, "report.semantic"):
            verify.validate(self.project)

    def test_readme_figures_and_generated_ci_inventory_cannot_lie(self):
        from unbake.report import verify

        self.exact()
        self.save()
        readme = self.project.root / "README.md"
        original = readme.read_text()
        readme.write_text(original.replace("12 of 36", "13 of 36"))
        with self.assertRaisesRegex(Held, "report.readme"):
            verify.validate(self.project)
        readme.write_text(original)
        workflow = self.project.root / ".github/workflows/progress.yml"
        workflow.write_text(workflow.read_text().replace("name: 'us_report'", "name: 'wrong_report'"))
        with self.assertRaisesRegex(Held, "report.ci"):
            verify.validate(self.project)

    def test_existing_manifest_duplicates_refuse_before_any_report_write(self):
        from unbake.report import verify

        self.retained()
        self.save()
        path = self.project.root / verify.MANIFEST
        original = path.read_bytes()
        path.write_bytes(original.replace(b'"schema": 1', b'"schema": 1, "schema": 1'))
        with (
            patch.object(progress, "readme_descriptions", return_value={v: f"{v} (fixture)" for v in self.versions}),
            patch("unbake.report.files.write") as writes,
            self.assertRaisesRegex(Held, "duplicate JSON key 'schema'"),
        ):
            progress.write(self.project, self.host)
        writes.assert_not_called()

    def test_ci_payload_executes_without_installed_packages_and_no_rom(self):
        from unbake.report import verify

        self.exact()
        self.retained()
        self.save()
        for version in self.versions:
            self.project.version(version).baserom.unlink()
        directory = self.root / "verifier"
        with zipfile.ZipFile(self.project.root / verify.BUNDLE) as archive:
            archive.extractall(directory)
        env = {**os.environ, "PYTHONPATH": str(directory), "GITHUB_SHA": "2" * 40}
        with patch.object(subprocess, "run", wraps=subprocess.run) as executions:
            completed = subprocess.run(
                [
                    sys.executable,
                    "-S",
                    "-m",
                    "unbake.report.verify",
                    "--project",
                    str(self.project.root),
                    "--artifacts",
                    str(self.project.root / "out"),
                ],
                cwd=self.project.root,
                env=env,
                capture_output=True,
                text=True,
            )
        self.assertEqual(executions.call_count, 1)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        with patch.object(progress, "readme_descriptions") as private_rom:
            progress.write(self.project, None, source_only=True)
        private_rom.assert_not_called()
        verify.validate(self.project)

    def public_progress_fixture(self):
        from unbake.cli.main import main

        values = toml.loads((self.project.root / "config.toml").read_text())
        for version, cartridge in (("us", "NUS-NRWP-0"), ("eu", "NUS-NRWE-0")):
            values["version"][version].update(cartridge_id=cartridge, region=version, description="Owner release.")
        (self.project.root / "config.toml").write_text(toml.dumps(values))
        self.project = config.load(self.project.root)
        values = host_values(self.root)
        values["resources"].update(
            domain="standalone", memory_total_bytes=1 << 30, memory_parent_bytes=1 << 29, memory_worker_bytes=1 << 28
        )
        host = self.root / "report-host.toml"
        host.write_text(toml.dumps(values))
        self.exact()
        self.retained(score=17)
        self.retained("gamma", score=None)
        # Begin with the same canonical committed summary that a prior publication writes.
        self.assertEqual(set(attempts.ledger(self.project).fuzzy_sources()), {"beta", "gamma"})
        buildfiles.write_progress(self.project, publish_branch="main")
        out, err = io.StringIO(), io.StringIO()
        from unbake import cdecl
        from unbake.report import state

        with (
            redirect_stdout(out),
            redirect_stderr(err),
            patch.object(state, "inventory", wraps=state.inventory) as inventories,
            patch.object(split, "functions", wraps=split.functions) as scans,
            patch.object(cdecl, "declarations", wraps=cdecl.declarations) as parses,
            patch.object(progress, "render", wraps=progress.render) as renders,
        ):
            code = main(["--project", str(self.project.root), "--config", str(host), "recompute", "progress"])
        self.assertEqual(code, 0, err.getvalue())
        result = json.loads(out.getvalue())
        self.assertEqual(result["status"], "ok")
        self.assertEqual([(row["step"], row["ran"]) for row in result["data"]["steps"]], [("progress", True)])
        self.assertEqual(
            (inventories.call_count, scans.call_count, parses.call_count, renders.call_count), (1, 2, 3, 1)
        )
        for version in self.versions:
            report = json.loads((self.project.root / "versions" / version / "report.json").read_text())
            self.assertEqual((report["measures"]["matched_code"], report["measures"]["total_code"]), (12, 36))
            self.assertEqual(sum(len(row["functions"]) for row in report["units"]), 3)
            self.assertEqual(report["units"][1]["functions"][0]["fuzzy_match_percent"], 17)
            self.assertNotIn("fuzzy_match_percent", report["units"][2]["functions"][0])

    def test_public_progress_cartridge_order_passes_independent_bundled_verifier(self):
        from unbake.report import verify

        self.public_progress_fixture()
        readme = (self.project.root / "README.md").read_text()
        self.assertEqual(re.findall(r"^\| ([\w-]+) \(", readme, re.M), ["eu", "us"])
        for version in self.versions:
            self.project.version(version).baserom.unlink()
        directory = self.root / "independent-verifier"
        with zipfile.ZipFile(self.project.root / verify.BUNDLE) as archive:
            archive.extractall(directory)
        completed = subprocess.run(
            [
                sys.executable,
                "-S",
                "-m",
                "unbake.report.verify",
                "--project",
                str(self.project.root),
                "--artifacts",
                str(self.project.root / "out"),
            ],
            cwd=self.project.root,
            env={**os.environ, "PYTHONPATH": str(directory), "GITHUB_SHA": "3" * 40},
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(len(list((self.project.root / "out").glob("*.manifest.json"))), 2)

    def test_source_only_regeneration_retains_owner_order_and_labels(self):
        from unbake.report import verify

        self.public_progress_fixture()
        readme = self.project.root / "README.md"
        original = readme.read_text().replace("Owner release.", "Reviewed owner label.")
        readme.write_text(original)
        for version in self.versions:
            self.project.version(version).baserom.unlink()
        with (
            patch.object(progress, "readme_descriptions") as rom_labels,
            patch.object(progress, "render", wraps=progress.render) as renders,
        ):
            progress.write(self.project, None, source_only=True)
        rom_labels.assert_not_called()
        self.assertEqual(renders.call_count, 1)
        self.assertEqual(readme.read_text(), original)
        verify.validate(self.project)

    def test_source_only_regeneration_preserves_unique_owner_version_rename(self):
        from unbake.report import verify

        self.public_progress_fixture()
        readme = self.project.root / "README.md"
        original = readme.read_text().replace("| eu (", "| owner-eu (").replace("<code>eu ", "<code>owner-eu ")
        readme.write_text(original)
        for version in self.versions:
            self.project.version(version).baserom.unlink()
        with (
            patch.object(progress, "readme_descriptions") as rom_labels,
            patch.object(progress, "render", wraps=progress.render) as renders,
        ):
            progress.write(self.project, None, source_only=True)
        rom_labels.assert_not_called()
        self.assertEqual(renders.call_count, 1)
        self.assertEqual(readme.read_text(), original)
        verify.validate(self.project)

    def test_owner_descriptions_require_unique_configured_progress_tables(self):
        from unbake.report import readme_layout

        self.public_progress_fixture()
        readme = (self.project.root / "README.md").read_text()
        before, block, after = readme_layout.section(readme)
        for extra in ("eu", "obsolete"):
            with self.subTest(extra), self.assertRaisesRegex(Held, "unexpected or duplicated owner label"):
                progress.owner_descriptions(self.project, before + block + f"\n| {extra} (extra) |\n" + after)

    def test_version_active_bodies_are_required_in_each_declared_holding(self):
        self.exact()
        source = self.project.src / "alpha.c"
        source.write_text("#ifdef VERSION_US\nint alpha(void) { return 1; }\n#endif\n")
        with self.assertRaisesRegex(Held, "source.membership.*VERSION eu"):
            self.reports()

    def test_cached_inventory_refuses_changed_receipt_source_or_version_input(self):
        from unbake.report import state

        self.retained()
        current = state.inventory(self.project)
        source = self.project.src / "beta.c"
        source.write_text(source.read_text().replace("return 2", "return 3"))
        with self.assertRaisesRegex(Held, "source.changed"):
            state.assert_current(self.project, current)
        with self.assertRaisesRegex(Held, "source.changed"):
            progress.measure(self.project, self.host, "us", current=current)

    def test_progress_input_key_tracks_receipts_members_layout_and_tool_payload(self):
        from unbake.report import verify

        baseline = progress.input_key(self.project)
        self.retained()
        receipt = progress.input_key(self.project)
        self.assertNotEqual(baseline, receipt)
        path = self.project.version("us").symbols
        path.write_text(path.read_text() + "inner = 0x80001004; // type:func\n")
        symbol = progress.input_key(self.project)
        self.assertNotEqual(receipt, symbol)
        (self.project.root / verify.BUNDLE).write_bytes(b"pin")
        self.assertNotEqual(symbol, progress.input_key(self.project))
        self.assertEqual(progress.input_key(self.project), progress.input_key(self.project))

    def test_build_generation_reads_verifier_payload_once_for_both_ci_renderers(self):
        from unbake.report import verify

        with (
            patch("unbake.decomp.original_asm.guard"),
            patch.object(buildfiles, "n64link_pin", return_value="pinned"),
            patch.object(verify, "bundle", wraps=verify.bundle) as payload,
        ):
            generated = buildfiles.generate(self.project, self.host)
        self.assertEqual(payload.call_count, 1)
        pin = hashlib.sha256(generated[self.project.root / verify.BUNDLE]).hexdigest()
        for path in (".github/workflows/progress.yml", ".gitlab-ci.yml"):
            self.assertIn(pin, generated[self.project.root / path].decode())

    def test_both_ci_renderers_pin_and_run_same_verifier_before_copies(self):
        from unbake.report import verify

        github = buildfiles.github_progress(self.project, self.host)
        gitlab = buildfiles.gitlab_progress(self.project)
        pin = hashlib.sha256(verify.bundle()).hexdigest()
        for rendered in (github, gitlab):
            self.assertIn(pin + "  tools/report-verifier.zip", rendered)
            self.assertEqual(rendered.count("python3 -m unbake.report.verify --project . --artifacts out"), 1)
            self.assertLess(rendered.index("unbake.report.verify"), rendered.index("cp versions/"))
            self.assertNotIn("baserom", rendered)
            for version in self.versions:
                self.assertEqual(rendered.count(f"cp versions/{version}/report.json out/{version}_report.json"), 1)
        for action in (buildfiles.CHECKOUT, buildfiles.UPLOAD_ARTIFACT):
            self.assertIn(action[0] + "@" + action[1], github)
        self.assertIn("@sha256:", gitlab)


@skipUnless(os.environ.get("UNBAKE_REPORT_SNAPSHOT"), "frozen public snapshot supplied by integration lane")
class FrozenSnapshotTests(ProjectCase):
    EXPECTED: ClassVar = {
        "de": (1104964, 661724, 4467, 3607, 2132, 13, 277644),
        "eu": (1111996, 665580, 4474, 3600, 2132, 13, 275508),
        "eu-x": (1114140, 666024, 4476, 3603, 2212, 14, 204036),
        "us": (1062244, 656560, 4233, 3548, 2132, 13, 320748),
        "us-rev1": (1132124, 691612, 4547, 3719, 2212, 14, 254468),
    }

    def test_frozen_full_inventory_and_default_vs_weighted_all(self):
        from unbake.report import state, verify

        project = config.load(Path(os.environ["UNBAKE_REPORT_SNAPSHOT"]))
        inventory = state.inventory(project)
        reports = {v: progress.measure(project, None, v, current=inventory) for v in project.versions}
        manifest = verify.document(project, inventory, reports)
        census = json.loads(Path(os.environ["UNBAKE_REPORT_CENSUS"]).read_bytes())
        for version, expected in self.EXPECTED.items():
            measures = reports[version]["measures"]
            values = manifest["versions"][version]
            self.assertEqual(
                (
                    measures["total_code"],
                    measures["matched_code"],
                    measures["total_functions"],
                    measures["matched_functions"],
                    values["draft_bytes"],
                    values["draft_functions"],
                    measures["total_data"],
                ),
                expected,
            )
            actual = []
            report_units = {unit["name"]: unit for unit in reports[version]["units"] if unit["functions"]}
            for row in inventory.units[version]:
                emitted = report_units[Path(row.path).name]["functions"]
                members = split.unit_members(row)
                self.assertEqual(len(emitted), len(members))
                for function, member in zip(emitted, members, strict=True):
                    self.assertEqual(
                        (function["name"], int(function["size"]), int(function["address"])),
                        (member.name, member.end - member.start, member.address),
                    )
                for member in split.unit_members(row):
                    category = (
                        "exact"
                        if row.kind == "c"
                        else "fuzzy"
                        if any(alias in inventory.receipts for alias in member.aliases)
                        else "asm"
                    )
                    actual.append((member.start, member.end - member.start, row.path, category, member.aliases))
            source = [row for row in census if row["version"] == version]
            self.assertAlmostEqual(values["draft_weighted_bytes"], 1663.148, places=3)
            self.assertEqual(values["unknown_similarity_bytes"], 80 if version in ("eu-x", "us-rev1") else 0)
            lower_bound = 100 * (measures["matched_code"] + values["draft_weighted_bytes"]) / measures["total_code"]
            self.assertAlmostEqual(measures["fuzzy_match_percent"], lower_bound, places=5)
            self.assertEqual(len(actual), len(source))
            for generated, observed in zip(actual, source, strict=True):
                self.assertEqual(
                    generated[:4], (observed["rom_start"], observed["bytes"], observed["unit"], observed["state"])
                )
                self.assertIn(observed["name"], generated[4])
        overall = progress._aggregate(reports)["measures"]
        self.assertEqual(
            (
                overall["total_code"],
                overall["matched_code"],
                overall["total_functions"],
                overall["matched_functions"],
                overall["total_data"],
            ),
            (5525468, 3341500, 22197, 18077, 1332404),
        )
        self.assertEqual(sum(v["draft_bytes"] for v in manifest["versions"].values()), 10820)
        self.assertEqual(sum(v["draft_functions"] for v in manifest["versions"].values()), 67)
        self.assertAlmostEqual(100 * overall["matched_code"] / overall["total_code"], 60.474515, places=6)
        public = json.loads(Path(os.environ["UNBAKE_REPORT_PUBLIC_PROJECT"]).read_bytes())
        self.assertEqual(public["default_version"], "us-rev1")
        self.assertNotIn("all", public["report_versions"])
        default = reports[public["default_version"]]["measures"]
        self.assertAlmostEqual(100 * default["matched_code"] / default["total_code"], 61.089775, places=6)
        self.assertNotEqual(default["matched_code_percent"], overall["matched_code_percent"])
