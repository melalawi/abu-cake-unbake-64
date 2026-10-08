"""Owner progress layouts and work counts on captured real report measures."""

import json
import re
import unittest
from pathlib import Path
from unittest.mock import patch

from unbake.report import progress, readme_layout

FIXTURES = Path(__file__).parent / "report"
# Keep labels, alignment before the bars, HTML, table headers and all prose in the comparison.
NUMBERS = re.compile(r"\[[#\-█▒░]{20}\] +[0-9]+\.[0-9]+%(?: \(~[0-9]+\.[0-9]+%\))? +[0-9,]+ of [0-9,]+")
FORBIDDEN = ("Retained drafts", "matching unknown", "Fuzzy % is known similarity", "All versions:")


def without_data(content):
    # The per-ROM total line is the one addition to the reference layout.
    return re.sub(r"<code>total +\[[^<]+</code><br>", "", re.sub(r"<br><code>data +\[[^<]+</code>", "", content))


def layout(content):
    return NUMBERS.sub("<figure>", without_data(content)).replace("<code>bytes", "<code>code ")


def payload(name):
    return json.loads((FIXTURES / f"{name}-measures.json").read_bytes())


def descriptions(block, reports):
    labels = dict(re.findall(r"^\| ([\w-]+) (\([^\n|]+) \|\r?$", block, re.MULTILINE))
    return {version: f"{version} {labels.get(version, labels.get('eu-mul'))}" for version in reports}


class ReadmeFormatTests(unittest.TestCase):
    def assert_art_only(self, rendered):
        for phrase in FORBIDDEN:
            self.assertNotIn(phrase, rendered)

    def test_reference_sections_update_only_figures_with_exact_work_counts(self):
        # Reference captures: BattleTanx 2ba9c0b; RageWars efd5f6e.
        for name in ("exampleone", "exampletwo"):
            with self.subTest(name):
                block = (FIXTURES / f"{name}-reference.golden").read_text()
                template = "# Owner title\n\n## Progress\n\n" + block + "\n## Owner footer\n\nKeep this.\n"
                reports = payload(name)
                labels = descriptions(block, reports)
                labels = {v: text + " Different configured text." for v, text in reversed(labels.items())}
                count = len(reports)
                with (
                    patch.object(progress, "progress", wraps=progress.progress) as generate,
                    patch.object(progress, "_replace_figures", wraps=progress._replace_figures) as replace,
                ):
                    rendered = progress.render(template, reports, descriptions=labels)
                generate.assert_not_called()
                self.assertEqual(replace.call_count, 2 * count + 1 if count > 1 else 1)
                self.assertEqual(layout(rendered), layout(template))
                self.assertEqual(len(NUMBERS.findall(rendered)), 5 * count + 1 if count > 1 else 4)
                self.assertNotEqual(rendered, template)
                self.assert_art_only(rendered)
                tables = re.findall(r"\| <pre>(.*?)</pre> \|", rendered, re.S)
                self.assertEqual(len(tables), count)
                for table in tables:
                    self.assertEqual(re.findall(r"<code>(\w+) +\[", table), ["total", "code", "data", "functions"])
                if count == 1:
                    # Function accounting is preserved: actual definitions, rather than translation units.
                    self.assertIn("138 of 3,234", rendered)
                else:
                    # The owner's old name still selects the configured eu-x report.
                    self.assertIn("| eu-mul (", rendered)
                    self.assertNotIn("| eu-x (", rendered)
                    self.assertIn("697,768 of 1,115,312", rendered)

    def test_actual_five_rom_overall_weights_code_and_verified_initialized_data(self):
        # Canonical RageWars 7d17ee3: only us-rev1 has verified DATA (89,714).
        reports = payload("ragewars-7d17")
        reference = (FIXTURES / "exampletwo-reference.golden").read_text()
        template = "# Owner\r\n\r\n## Progress\r\n\r\n" + reference.replace("\n", "\r\n")
        template += "\r\n## Footer\r\nKeep owner prose.\r\n"
        labels = descriptions(reference, reports)
        rendered = progress.render(template, reports, descriptions=labels)
        self.assertEqual(layout(rendered), layout(template))
        self.assertEqual(progress._figures(progress._aggregate(reports), "all")[:2], (3566938, 6857872))
        self.assertIn("52.01% (~52.34%)  3,566,938 of 6,857,872 bytes", rendered)
        self.assertIn("89,714 of 254,468", rendered)
        self.assertEqual(rendered.count("<code>data"), 5)
        self.assertIn("778,116 of 1,132,124", rendered)
        self.assertEqual(progress.render(rendered, reports, descriptions=labels), rendered)
        self.assertEqual(reports, payload("ragewars-7d17"))
        generated = progress.progress(reports, labels)
        self.assertIn("3,566,938 of 6,857,872 bytes", generated)

    def test_new_section_generates_the_reference_format_without_accounting_prose(self):
        for name in ("exampleone", "exampletwo"):
            with self.subTest(name):
                reference = (FIXTURES / f"{name}-reference.golden").read_text()
                reports = payload(name)
                # Reference uses the owner's former name for the same multi-language release.
                reports = {"eu-mul" if v == "eu-x" else v: report for v, report in reports.items()}
                for report in reports.values():
                    report["measures"]["total_data"] = 1234
                    report["measures"]["complete_data"] = 456
                    report["measures"]["matched_data"] = 456
                    report["categories"] = [{"id": "draft", "measures": {"total_code": 64, "total_functions": 2}}]
                labels = descriptions(reference, reports)
                generated = progress.progress(reports, labels)
                self.assertEqual(layout(generated) + "\n", layout(reference))
                self.assert_art_only(generated)
                self.assertEqual(generated.count("456 of 1,234"), len(reports))
                template = "# Owner\n\n## Progress\n\n\n## Next\n"
                with patch.object(progress, "progress", wraps=progress.progress) as generate:
                    rendered = progress.render(template, reports, descriptions=labels)
                self.assertEqual(generate.call_count, 1)
                self.assertEqual(readme_layout.section(rendered)[1], generated + "\n")
                self.assert_art_only(rendered)

    def test_owner_edits_and_line_endings_survive_update_and_following_render(self):
        for name, readme in (("exampleone", "ExampleOne"), ("exampletwo", "ExampleTwo")):
            for newline in ("\n", "\r\n"):
                with self.subTest(name=name, newline=repr(newline)):
                    original = (FIXTURES / "readme" / f"{readme}.md").read_text()
                    original = original.replace("## Progress\n\n", "## Progress\n\nOwner introduction.\n\n")
                    original = original.replace("</pre> |", "</pre> |\n<!-- Owner caption. -->")
                    original = original.replace("|---|", "| --- |")
                    original = original.replace("## Development", "Owner conclusion.\n\n## Development")
                    original = original.replace("\n", newline)
                    reports = payload(name)
                    labels = descriptions(readme_layout.section(original)[1], reports)
                    with patch.object(progress, "progress", wraps=progress.progress) as generate:
                        rendered = progress.render(original, reports, descriptions=labels)
                        following = progress.render(rendered, reports, descriptions=labels)
                    generate.assert_not_called()
                    self.assertNotEqual(rendered, original)
                    self.assertEqual(layout(rendered).encode(), layout(original).encode())
                    self.assertEqual(following.encode(), rendered.encode())
                    self.assert_art_only(rendered)


if __name__ == "__main__":
    unittest.main()
