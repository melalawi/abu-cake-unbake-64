"""Native reports and the byte-exact project Progress format."""

import argparse
import json
import os
import struct
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from tests.project.makefile_fixture import fixture
from tests.support import test_policy, tool
from unbake.cli import check
from unbake.decomp import score
from unbake.project import init
from unbake.project.config import Held
from unbake.project.header import Header
from unbake.project.rom import Rom
from unbake.report import progress as report
from unbake.report import units as report_units


def document(code: int, total: int, matched_percent: float, fuzzy_percent: float) -> dict[str, Any]:
    return {
        "version": 2,
        "measures": {
            "complete_code": code,
            "total_code": total,
            "complete_code_percent": matched_percent,
            "fuzzy_match_percent": fuzzy_percent,
        },
    }


class RenderTests(unittest.TestCase):
    def test_version_rows_preserve_layout_and_only_replace_figures(self) -> None:
        for version, padding, old, complete, fuzzy, percent in (
            ("us", " ", "2.11", 7200, 77.6, " 72.00"),
            ("us", " ", "2.11", 10000, 100, " 100.00"),
            ("us-rev1", "   ", "71.09", 10000, 100, " 100.00"),
        ):
            with self.subTest(version=version):
                original = (
                    "intro\n## Progress\n\n"
                    f"| {version} (release description) |\n|---|\n"
                    f"| <pre><code>{version}{padding}[{'░' * 20}]  {old}% (~0.00%)"
                    "  0 of 10,000 bytes</code></pre> |\n\n## End\nfooter\n"
                )
                bar = "██████████████▒▒░░░░" if complete == 7200 else "█" * 20
                expected = original.replace("░" * 20, bar).replace(
                    f"  {old}% (~0.00%)  0", f"{percent}% (~{fuzzy:.2f}%)  {complete:,}"
                )
                reports = {version: document(complete, 10000, 0, fuzzy)}
                self.assertEqual(report.render(original, reports), expected)
                self.assertEqual(report.render(expected, reports), expected)

    def test_reference_layout_goldens_and_idempotence(self) -> None:
        for stem in ("battletanx", "ragewars"):
            with self.subTest(project=stem):
                root = Path(__file__).parent
                reference = (root / f"{stem}-reference.golden").read_text()
                golden = (root / f"{stem}-progress.golden").read_text()
                reports = json.loads((root / f"{stem}-measures.json").read_text())
                template = "intro\n## Progress\n\n" + reference + "\n## End\nfooter\n"
                expected = template.replace(reference, golden)
                self.assertEqual(report.render(template, reports), expected)
                self.assertEqual(report.render(expected, reports), expected)
                self.assertNotIn("fuzzy", golden)
                # Configuration order cannot change existing README VERSION order.
                self.assertEqual(report.render(template, dict(reversed(list(reports.items())))), expected)

    def test_byte_and_function_bar_edges(self) -> None:
        cases = (
            (0, 0, "░" * 20),
            (100, 100, "█" * 20),
            (71, 71, "█" * 14 + "░" * 6),
            (71.09, 77.60, "██████████████▒▒░░░░"),
            (99, 100, "█" * 19 + "▒"),
        )
        for matched, fuzzy, bar in cases:
            with self.subTest(matched=matched, fuzzy=fuzzy):
                candidate = document(round(matched * 100), 10000, matched, fuzzy)
                candidate["measures"].update(complete_units=round(matched * 100), total_units=10000)
                rendered = report.progress({"us": candidate}, {"us": "us (release)"})
                self.assertIn(f"bytes     [{bar}]  {matched:5.2f}% (~{fuzzy:.2f}%)", rendered)
                self.assertIn(f"functions [{'█' * int(matched // 5) + '░' * (20 - int(matched // 5))}]", rendered)
                self.assertEqual(rendered.count("(~"), 1)
        empty = report.progress({"us": document(0, 0, 0, 0)}, {"us": "us (release)"})
        self.assertIn("0 of 0", empty)

    def test_summary_totals_are_weighted_and_padding_is_preserved(self) -> None:
        summary = (
            "<pre><code>all     [--------------------]   0.00%  0 of 400 bytes</code><br>"
            "<code>us      [--------------------]   0.00%  0 of 100 bytes</code><br>"
            "<code>us-rev1 [--------------------]   0.00%  0 of 300 bytes</code></pre>\n\n"
        )
        reports = {"us": document(50, 100, 50, 75), "us-rev1": document(300, 300, 100, 100)}
        tables = report.progress(reports, {v: v + " (release)" for v in reports})
        template = "## Progress\n\n" + summary + tables + "\n\n## End\n"
        rendered = report.render(template, reports)
        self.assertIn("all     [█████████████████▒▒░]  87.50% (~93.75%)  350 of 400 bytes", rendered)
        self.assertEqual(rendered.count("<pre>"), 3)
        self.assertEqual(rendered.count("functions"), 2)
        self.assertEqual(report.render(rendered, reports), rendered)

    def test_completion_counters_override_matched_measures(self) -> None:
        candidate = document(7, 100, 71.09, 77.60)
        candidate["measures"].update(
            matched_code="7",
            complete_code="90",
            complete_code_percent=90,
            complete_units="99",
            total_units=100,
            matched_functions="3",
            total_functions="10",
            matched_functions_percent=30,
        )
        rendered = report.progress({"us": candidate}, {"us": "us (release)"})
        self.assertIn("bytes     [██████████████████░░]  90.00% (~77.60%)  90 of 100", rendered)
        self.assertIn("functions [███████████████████░]  99.00%  99 of 100", rendered)

    def test_required_measures_and_descriptions_are_named(self) -> None:
        for field, values in (
            ("complete_code", (-1, "bad", True)),
            ("total_code", (-1, "bad", True)),
            ("complete_units", (-1, "bad", True)),
            ("total_units", (-1, "bad", True)),
            ("fuzzy_match_percent", (-1, 101, "0", True, float("nan"), float("inf"))),
        ):
            for value in values:
                with self.subTest(field=field, value=value):
                    candidate = document(0, 0, 0, 0)
                    candidate["measures"][field] = value
                    with self.assertRaisesRegex(Held, field):
                        report.progress({"us": candidate}, {"us": "us (fixture)"})
        for reports, descriptions, field in (
            ({}, {}, "VERSION"),
            ({"us": {"version": 1}}, {"us": "us (fixture)"}, "version"),
            ({"us": {"version": 2}}, {"us": "us (fixture)"}, "measures"),
            ({"us": document(2, 1, 0, 0)}, {"us": "us (fixture)"}, "exceeds"),
            ({"us": document(0, 0, 0, 0)}, {}, "descriptions.us"),
            ({"us": document(0, 0, 0, 0)}, {"us": "bad|description"}, "description"),
        ):
            with self.subTest(field=field), self.assertRaisesRegex(Held, field):
                report.progress(reports, descriptions)

    def test_missing_progress_structure_and_removed_format_are_named(self) -> None:
        for template, field in (
            ("No heading", "Progress"),
            ("## Progress\n\n", "following section"),
            ("## Progress\n\nold\n## End\n", "descriptions.us"),
            ("## Progress\n\n<pre>summary</pre>\n| us (release) |\n\n## End\n", "progress block"),
            (
                "## Progress\n\n| us (release) |\n| <pre><code>wrong ["
                + "░" * 20
                + "]  0.00% (~0.00%)  0 of 100 bytes</code></pre> |\n\n## End\n",
                "unexpected label",
            ),
        ):
            with self.subTest(template=template), self.assertRaisesRegex(Held, field):
                report.render(template, {"us": document(0, 0, 0, 0)})


class ReportTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir=os.environ["TMPDIR"])
        self.addCleanup(self.temporary.cleanup)
        self.addCleanup(patch.stopall)
        self.root = Path(self.temporary.name)
        project, _ = fixture(self.root / "project")
        self.policy = test_policy(self.root)
        self.project = replace(project, name="fixture")
        version = self.project.version("us")
        version.split.write_text(
            "segments:\n  - name: code\n    type: code\n    start: 0\n"
            "    vram: 0x80000000\n    subalign: 4\n    subsegments:\n"
            "      - [0, c, matched]\n      - [12, asm, draft]\n"
            "      - [24, asm, untouched]\n  - [36]\n"
        )
        version.baserom.write_bytes(struct.pack(">III", 0x24020001, 0x03E00008, 0) * 3)
        (self.project.src / "matched.c").write_text("void matched(void) {}\n")
        (self.project.src / "draft.c").write_text("#ifdef NON_MATCHING\nvoid draft(void) {}\n#endif\n")
        self.readme = self.project.root / "README.md"
        self.readme.write_text(
            "Project introduction\n\n## Progress\n\n| us (fixture release) |\n|---|\n"
            "| <pre><code>bytes     [--------------------]   0.00%  0 of 36</code><br>"
            "<code>functions [--------------------]   0.00%  0 of 3</code></pre> |\n\n## Contributions\nGuide\n"
        )
        self.generation = self.project.root / "build/us.1"
        self.object(self.generation / "obj/src/matched.o", "matched")
        for function in ("draft", "untouched"):
            self.object(self.generation / "obj/asm" / (function + ".o"), function)
        self.partial = self.project.root / "build/us.nonmatching/obj/src/draft.o"
        self.object(self.partial, "draft")
        self.project.build_link("us").symlink_to(self.generation.name)
        score.verified.clear()
        self.addCleanup(score.verified.clear)

    def object(self, path: Path, function: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        body = (
            f".text\n.set noreorder\n.globl {function}\n.type {function},@function\n{function}:\n"
            f"addiu $v0,$zero,1\njr $ra\nnop\n.size {function},.-{function}\n"
        )
        subprocess.run(
            [tool("mips-linux-gnu-as"), "-EB", "-mips3", "-o", str(path)],
            input=body,
            capture_output=True,
            text=True,
            check=True,
        )

    def test_check_refuses_stale_totals_without_publishing_and_report_repairs_them(self) -> None:
        destination = self.project.root / "versions/us/report.json"
        report.write(self.project, self.policy)
        with patch("unbake.cli.check.hygiene.tracked_findings", side_effect=lambda *_args: []):
            self.assertFalse(check.run(argparse.Namespace(hygiene=False), self.project, self.policy))
            split = self.project.version("us").split
            split.write_text(split.read_text().replace("[12, asm, draft]", "[12, c, draft]"))
            self.object(self.generation / "obj/src/draft.o", "draft")
            before = destination.read_bytes(), self.readme.read_bytes()
            self.assertTrue(check.run(argparse.Namespace(hygiene=False), self.project, self.policy))
            self.assertEqual(
                report.findings(self.project, self.policy), ["HELD(check): stale report VERSION us: run unbake report"]
            )
            self.assertEqual((destination.read_bytes(), self.readme.read_bytes()), before)
            report.write(self.project, self.policy)
            self.assertFalse(check.run(argparse.Namespace(hygiene=False), self.project, self.policy))
            current = destination.read_bytes()
            for field in ("complete_code", "total_code", "complete_units", "total_units", "fuzzy_match_percent"):
                with self.subTest(field=field):
                    document = json.loads(current)
                    document["measures"][field] += 1
                    destination.write_text(json.dumps(document))
                    self.assertIn("stale report VERSION us", report.findings(self.project, self.policy)[0])
            destination.write_bytes(current)
            self.assertEqual(report.findings(self.project, self.policy), [])

    def test_native_reports_use_partial_objects_and_project_versions(self) -> None:
        written = report.write(self.project, self.policy)
        self.assertEqual(len(written), 3)
        self.assertIn(self.project.root / "versions/us/report.json", written)
        self.assertFalse((self.policy.state_root / "fixture/reports").exists())
        configuration = json.loads((self.generation / "objdiff.json").read_text())
        units = configuration["units"]
        self.assertEqual([unit["metadata"]["complete"] for unit in units], [True, False, False])
        self.assertEqual((self.generation / units[1]["base_path"]).resolve(), self.partial)
        self.assertNotIn("base_path", units[2])
        self.assertEqual((self.generation / units[1]["target_path"]).resolve(), self.generation / "obj/asm/draft.o")
        destination = self.project.root / "versions/us/report.json"
        result = json.loads(destination.read_text())
        self.assertEqual(result["measures"]["complete_units"], 1)
        self.assertEqual(result["measures"]["total_units"], 3)
        self.assertFalse((self.project.root / "data").exists())
        self.assertTrue(self.readme.read_text().startswith("Project introduction\n\n## Progress\n"))
        self.assertTrue(self.readme.read_text().endswith("## Contributions\nGuide\n"))
        self.assertNotIn("old figures", self.readme.read_text())

    def test_cross_version_c_is_not_an_assembly_versions_draft(self) -> None:
        original = self.project.version("us")
        other_split = self.root / "other.yaml"
        other_split.write_text(original.split.read_text())
        other = replace(original, name="eu", split=other_split)
        project = replace(self.project, versions=("us", "eu"), version_map={"us": original, "eu": other})
        original.split.write_text(original.split.read_text().replace("[0, c, matched]", "[0, asm, matched]"))
        self.object(self.generation / "obj/asm/matched.o", "matched")
        self.object(project.root / "build/eu.nonmatching/obj/src/draft.o", "draft")
        stale = project.root / "build/us.nonmatching/obj/src/matched.o"
        for stale_present in (False, True):
            if stale_present:
                self.object(stale, "matched")
            for version, complete in (("us", False), ("eu", True)):
                with self.subTest(version=version, stale_present=stale_present):
                    rows = report_units.units(project, self.policy, version, self.generation, self.root / "report")
                    self.assertEqual(rows[0]["metadata"]["complete"], complete)
                    self.assertEqual("base_path" in rows[0], complete)
                    self.assertIn("base_path", rows[1])
                    self.assertNotIn("base_path", rows[2])

    def test_partial_report_measures_objects_without_linking_undefined_drafts(self) -> None:
        subprocess.run(
            [tool("mips-linux-gnu-as"), "-EB", "-mips3", "--no-pad-sections", "-o", str(self.partial)],
            input=(
                ".text\n.set noreorder\n.globl draft\n.type draft,@function\ndraft:\n"
                "lui $v0,%hi(undefined_draft_symbol)\njr $ra\n"
                "addiu $v0,$v0,%lo(undefined_draft_symbol)\n.size draft,.-draft\n"
            ),
            text=True,
            capture_output=True,
            check=True,
        )
        partial_build = self.partial.parents[2]
        self.assertEqual(list(partial_build.glob("*.elf")), [])
        report.write(self.project, self.policy)
        configuration = json.loads((self.generation / "objdiff.json").read_text())
        self.assertEqual((self.generation / configuration["units"][1]["base_path"]).resolve(), self.partial)
        self.assertEqual(list(partial_build.glob("*.elf")), [])
        self.partial.unlink()
        with self.assertRaisesRegex(Held, r"make -j4 VERSION=us NON_MATCHING=1 build/us.nonmatching/obj/src/draft.o"):
            report.write(self.project, self.policy)

    def test_report_publishes_in_project_without_state_writes(self) -> None:
        state = self.policy.state_root
        state.mkdir(parents=True)
        sentinel = state / "existing.json"
        sentinel.write_bytes(b"existing state\n")
        before = {path.relative_to(state): path.read_bytes() for path in state.rglob("*") if path.is_file()}
        destination = self.project.root / "versions/us/report.json"
        destination.write_bytes(b"stale report\n")
        units = [
            {
                "name": "matched",
                "base_path": "obj/src/matched.o",
                "target_path": "obj/src/matched.o",
                "metadata": {"complete": True},
            }
        ]
        with patch.object(report_units, "units", return_value=units):
            written = report.write(self.project, self.policy)
        self.assertIn(destination, written)
        self.assertEqual(json.loads(destination.read_bytes())["version"], 2)
        after = {path.relative_to(state): path.read_bytes() for path in state.rglob("*") if path.is_file()}
        self.assertEqual(after, before)
        self.assertEqual(list(state.iterdir()), [sentinel])

    def test_native_report_accepts_readme_created_by_init(self) -> None:
        cartridge = Rom(
            self.project.version("us").baserom,
            b"",
            Header(0x80371240, 0, 0x80000000, 0, 0, "", 0, 0, "Example", "N", "EX", "E", 0, "6102/7101"),
            "0" * 40,
        )
        init.readme(self.project, "Example", [cartridge], {cartridge.path: "us"})
        self.assertIn("0 of 36", self.readme.read_text())
        report.write(self.project, self.policy)
        self.assertIn("| us (us, revision 0) |", self.readme.read_text())
        self.assertIn("12 of 36", self.readme.read_text())

    def test_target_wrapper_scores_same_code_and_names_bad_input(self) -> None:
        target = self.root / "target.o"
        target.write_bytes(report.target_object("draft", struct.pack(">III", 0x24020001, 0x03E00008, 0)))
        self.assertEqual(score.fuzzy(self.project, self.policy, "us", "draft", target, self.partial), 100.0)
        for name, code, field in (("", b"1234", "function"), ("draft", b"bad", "whole MIPS words")):
            with self.subTest(field=field), self.assertRaisesRegex(Held, field):
                report.target_object(name, code)

    def test_missing_build_objects_are_named(self) -> None:
        for path, field in (
            (self.generation / "obj/src/matched.o", "matched.*linked src object"),
            (self.partial, "partial src object.*NON_MATCHING=1"),
        ):
            with self.subTest(path=path):
                content = path.read_bytes()
                path.unlink()
                with self.assertRaisesRegex(Held, field):
                    report.write(self.project, self.policy)
                path.write_bytes(content)

    def test_cli_failure_preserves_report_and_readme(self) -> None:
        destination = self.project.root / "versions/us/report.json"
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text("existing report")
        before = self.readme.read_bytes()
        with (
            patch(
                "unbake.report.progress.subprocess.run",
                return_value=SimpleNamespace(returncode=1, stderr="invalid report input", stdout=""),
            ),
            self.assertRaisesRegex(Held, "invalid report input"),
        ):
            report.write(self.project, self.policy)
        self.assertEqual(self.readme.read_bytes(), before)
        self.assertEqual(destination.read_text(), "existing report")
