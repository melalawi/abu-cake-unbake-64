"""Status guidance never presents an ineligible unmatched body as the next draft."""

import json
from unittest.mock import patch

from tests.kit import TempCase
from tests.project_fixture import make
from unbake import pool
from unbake.work import explain, plan


class ExplainEligibilityTests(TempCase):
    def test_status_preserves_reasons_and_only_recommends_eligible_drafts(self):
        for label, words, excluded, eligible in (
            ("eligible", [0x24020001, 0x03E00008, 0], False, True),
            ("indirect", [0x00800008, 0, 0x03E00008, 0], False, False),
            ("excluded", [0x24020001, 0x03E00008, 0], True, False),
        ):
            with self.subTest(label=label):
                project, host = make(self.root / label, words)
                if excluded:
                    (project.root / "unbake-exclusions.json").write_text(
                        json.dumps({"schema": 1, "functions": ["alpha"]})
                    )
                with patch.object(pool, "run", side_effect=lambda host, fn, jobs: [fn(job) for job in jobs]):
                    report = explain.explain(project, host, "alpha", ("status",))
                self.assertEqual(report.next_words, ("draft", "alpha") if eligible else None)
                status = report.sections["status"]
                self.assertEqual(status["cycle"]["eligible"], eligible)
                if label == "indirect":
                    self.assertIn("unresolved-indirect-jump-table-ownership", status["cycle"]["reason"])
                if excluded:
                    self.assertIn("excluded", status["cycle"]["reason"])

    def test_existing_draft_remains_comparable_when_cycle_refuses_the_body(self):
        project, host = make(self.root, [0x00800008, 0, 0x03E00008, 0])
        file = project.work / "alpha" / "alpha.c"
        file.parent.mkdir(parents=True)
        file.write_text("int alpha(void) { return 1; }\n")
        with patch.object(pool, "run", side_effect=lambda host, fn, jobs: [fn(job) for job in jobs]):
            report = explain.explain(project, host, "alpha", ("status",))
        self.assertEqual(report.next_words, ("compare", str(file)))
        self.assertFalse(report.sections["status"]["cycle"]["eligible"])

    def test_named_selection_is_the_same_subset_without_the_size_window(self):
        project, host = make(self.root)
        with patch.object(pool, "run", side_effect=lambda host, fn, jobs: [fn(job) for job in jobs]):
            all_candidates = plan.candidates(project, host)
            selected = plan.candidates(project, host, selected=frozenset({"alpha"}))
        self.assertEqual({row.function for row in all_candidates}, {"alpha", "beta", "gamma"})
        self.assertEqual(selected, [row for row in all_candidates if row.function == "alpha"])
        self.assertLess(selected[0].bytes, host.cycle_min_bytes)

    def test_nonstatus_section_leads_to_status_before_recommending_a_draft(self):
        project, host = make(self.root)
        with patch.object(explain, "_types", return_value="typedef int s32;"):
            report = explain.explain(project, host, "alpha", ("types",))
        self.assertEqual(report.sections, {"types": "typedef int s32;"})
        self.assertEqual(report.next_words, ("explain", "alpha", "--section", "status"))
