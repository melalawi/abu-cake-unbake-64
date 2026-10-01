"""Native reports and the byte-exact project Progress format."""

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

from tests.project.test_makefile import fixture
from tests.support import test_policy, tool
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
            "matched_code": code,
            "total_code": total,
            "matched_code_percent": matched_percent,
            "fuzzy_match_percent": fuzzy_percent,
        },
    }


class RenderTests(unittest.TestCase):
    def test_exact_example_and_rounding_edges(self) -> None:
        example = Path(__file__).with_name("example-progress.golden").read_text().rstrip("\n")
        cases = (
            ("us-rev1", document(805984, 1133680, 71.09, 77.60), example),
            ("us", document(0, 100, 0, 0), "us [" + "░" * 20 + "]  0.00% (~0.00%)  0 of 100 bytes"),
            ("us", document(100, 100, 100, 100), "us [" + "█" * 20 + "]  100.00% (~100.00%)  100 of 100 bytes"),
            ("us", document(71, 100, 71, 71), "us [" + "█" * 14 + "░" * 6 + "]  71.00% (~71.00%)  71 of 100 bytes"),
            ("us", document(0, 0, 0, 0), "us [" + "░" * 20 + "]  0.00% (~0.00%)  0 of 0 bytes"),
            ("us", document(99, 100, 99, 100), "us [" + "█" * 19 + "▒" + "]  99.00% (~100.00%)  99 of 100 bytes"),
        )
        for version, measures, expected in cases:
            with self.subTest(version=version, measures=measures):
                self.assertEqual(report.progress_line(version, measures), expected)

    def test_real_battletanx_progress_format(self) -> None:
        golden = Path(__file__).with_name("battletanx-progress.golden").read_text().rstrip("\n")
        measures = json.loads(Path(__file__).with_name("battletanx-measures.json").read_text())
        original = (Path(__file__).parents[1] / "fixture/readme/BattleTanx.golden").read_text()
        before, body = original.split("## Progress\n\n")
        _, after = body.split("\n\n## ", 1)
        expected = before + "## Progress\n\n" + golden + "\n\n## " + after
        updated = report.render(original, {"us": measures})
        self.assertEqual(updated, expected)
        self.assertEqual(report.render(updated, {"us": measures}), expected)
        self.assertNotIn("fuzzy", updated.split("## Progress")[1].split("\n## ")[0])

    def test_configuration_order_and_surrounding_markup(self) -> None:
        reports = {"us-rev1": document(2, 4, 50, 75), "us": document(1, 4, 25, 25)}
        descriptions = {version: version + " (release)" for version in reports}
        tables = report.progress(reports, descriptions)
        self.assertLess(tables.index("<code>us-rev1"), tables.index("<code>us ["))
        old = "\n\n".join(reversed(tables.split("\n\n"))).replace("<code>us-rev1 [", "<code>bytes [", 1)
        template = "intro\n## Progress\n\n<!-- progress -->\n" + old + "\n<!-- end -->\n\n## End\nfooter\n"
        self.assertEqual(report.render(template, reports), template.replace(old, tables))

    def test_native_measures_are_used_without_completed_counters(self) -> None:
        candidate = document(7, 100, 71.09, 77.60)
        candidate["measures"].update(matched_code="7", complete_code=90, complete_code_percent=90)
        self.assertEqual(
            report.progress_line("us-rev1", candidate),
            "us-rev1 [██████████████▒▒░░░░]  71.09% (~77.60%)  7 of 100 bytes",
        )

    def test_legacy_multiversion_summary_is_replaced_by_one_line_per_version(self) -> None:
        original = (Path(__file__).parents[1] / "fixture/readme/RageWars.golden").read_text()
        versions = ("us", "us-rev1", "eu", "eu-mul", "de")
        reports = {version: document(0, 100, 0, 0) for version in versions}
        rendered = report.render(original, reports)
        body = rendered.split("## Progress\n\n")[1].split("\n## ")[0]
        self.assertEqual(body.count("<code>"), len(versions))
        self.assertEqual(body.count("<pre>"), len(versions))
        self.assertNotIn("<pre></pre>", body)
        self.assertTrue(body.startswith("| us ("))
        self.assertEqual(report.render(rendered, reports), rendered)
        self.assertNotIn("functions", body)
        self.assertNotIn("<code>all", body)
        self.assertEqual(rendered.split("\n## Development")[1], original.split("\n## Development")[1])

    def test_required_measures_and_descriptions_are_named(self) -> None:
        for field, values in (
            ("matched_code", (-1, "bad", True)),
            ("total_code", (-1, "bad", True)),
            ("matched_code_percent", (-1, 101, "0", True, float("nan"), float("inf"))),
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
        self.assertEqual(
            report.progress_line("us", {"version": 2, "measures": {}}),
            "us [" + "░" * 20 + "]  0.00% (~0.00%)  0 of 0 bytes",
        )

    def test_missing_progress_structure_is_named(self) -> None:
        for template, field in (
            ("No heading", "Progress"),
            ("## Progress\n\n", "following section"),
            ("## Progress\n\nold\n## End\n", "descriptions.us"),
            ("## Progress\n\n<pre>summary</pre>\n| us (release) |\n\n## End\n", "progress block"),
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
            "| <pre><code>old figures</code></pre> |\n\n## Contributions\nGuide\n"
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
        self.assertIn("24 of 36", self.readme.read_text())

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
