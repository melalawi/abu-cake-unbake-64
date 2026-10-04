"""One reasoned action from current type, draft and trial evidence."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tests.match.support import MatchFixture
from unbake.cli import workflow
from unbake.decomp import draft_presence, work
from unbake.decomp.trial_compile import default_scratch


class WorkflowTests(MatchFixture):
    def setUp(self) -> None:
        super().setUp()
        context = patch.object(workflow.type_context, "required", return_value=("d" * 64, ""))
        context.start()
        self.addCleanup(context.stop)

    def types(self) -> None:
        for name in ("map/facts.json", "types/database.json"):
            path = self.project.build / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("{}\n")

    def editable(self):
        path = self.project.drafts / "alpha/alpha.c"
        path.parent.mkdir(parents=True)
        path.write_text("int alpha(void) { return 0; }\n")
        work.persist(self.project, work.identity(self.project, path, list(self.versions), policy=self.policy))
        return path

    def test_map_then_solve_precede_function_drafts(self) -> None:
        action, reason = workflow.select(self.project, self.policy)
        self.assertTrue(action.endswith(" map"), action)
        self.assertIn("whole-program", reason)
        path = self.project.build / "map/facts.json"
        path.parent.mkdir(parents=True)
        path.write_text("{}\n")
        action, reason = workflow.select(self.project, self.policy)
        self.assertTrue(action.endswith(" solve"), action)
        self.assertIn("shared type", reason)

    def test_latest_exact_trial_and_untried_edit_choose_different_actions(self) -> None:
        self.types()
        source = self.editable()
        self.prove(source)
        action, reason = workflow.select(self.project, self.policy)
        self.assertIn(" submit ", action)
        self.assertIn("every holding version", reason)
        source.write_text(source.read_text() + "/* edited */\n")
        action, reason = workflow.select(self.project, self.policy)
        self.assertIn(" try ", action)
        self.assertIn("inputs changed", reason)
        self.assert_untouched()

    def test_changed_types_request_redraft_before_old_exact_submission(self) -> None:
        self.types()
        source = self.editable()
        self.prove(source)
        with patch.object(workflow.type_context, "redrafts", return_value={"alpha": {"reasons": ["callee type"]}}):
            action, reason = workflow.select(self.project, self.policy)
        self.assertTrue(action.endswith(" draft alpha"), action)
        self.assertIn("redraft required", reason)

    def test_new_skips_redrafts_editable_and_retained_drafts_in_rank_order(self) -> None:
        self.types()
        source = self.editable()
        rows = [
            SimpleNamespace(function=name, aliases=(name,), versions=self.versions, size=16, score=None, draft=draft)
            for name, draft in (("alpha", None), ("retained", source), ("beta", None), ("gamma", None))
        ]
        with (
            patch.object(workflow.type_context, "redrafts", return_value={"alpha": {}}),
            patch.object(workflow.plan, "actionable", return_value=rows),
        ):
            action, _ = workflow.select(self.project, self.policy)
            self.assertTrue(action.endswith(" draft alpha"), action)
            for _ in range(2):
                action, reason = workflow.select(self.project, self.policy, new=True)
                self.assertTrue(action.endswith(" draft beta"), action)
                self.assertIn("alpha (editable draft), retained (retained draft)", reason)
            with patch.object(workflow.type_context, "redrafts", return_value={}):
                action, _ = workflow.select(self.project, self.policy)
                self.assertIn(" try ", action)
        with (
            patch.object(workflow.plan, "actionable", return_value=rows[:2]),
            patch.object(workflow.plan, "ranked", return_value=rows[:2]),
        ):
            action, reason = workflow.select(self.project, self.policy, new=True)
            self.assertEqual(action, "No undrafted items.")
            self.assertIn("undrafted", reason)
        self.assertTrue(Path(source).is_file())
        self.assert_untouched()

    def test_new_skips_private_and_failed_attempts_across_aliases_and_reports_reasons(self) -> None:
        self.types()
        scratch = self.root.parent / "private work"
        draft_presence.remember(self.project, "alternate", scratch, "private draft")
        draft_presence.remember(self.project, "beta", scratch, "failed draft attempt")
        rows = [
            SimpleNamespace(function=name, aliases=aliases, versions=self.versions, size=16, score=None, draft=None)
            for name, aliases in (("alpha", ("alpha", "alternate")), ("beta", ("beta",)), ("gamma", ("gamma",)))
        ]
        before = {p: p.read_bytes() for p in self.project.drafts.rglob("*.json")}
        with patch.object(workflow.plan, "actionable", return_value=rows):
            action, reason = workflow.select(self.project, self.policy, new=True)
        self.assertTrue(action.endswith(" draft gamma"), action)
        self.assertIn("Skipped: alpha (private draft), beta (failed draft attempt)", reason)
        self.assertEqual(before, {p: p.read_bytes() for p in self.project.drafts.rglob("*.json")})

    def test_new_discovers_legacy_default_private_manifest_and_ignores_other_workspace(self) -> None:
        import json

        self.types()
        directory = default_scratch(self.project, self.policy) / "drafts/alpha"
        directory.mkdir(parents=True)
        source = directory / "alpha.c"
        source.write_text("int alpha(void) { return 0; }\n")
        manifest = {
            "schema": 1,
            "project_id": self.project.id,
            "workspace_id": self.project.workspace_id,
            "subject": "alpha",
        }
        path = directory / "manifest.json"
        path.write_text(json.dumps(manifest))
        rows = [
            SimpleNamespace(function=name, aliases=(name,), versions=self.versions, size=16, score=None, draft=None)
            for name in ("alpha", "beta")
        ]
        with patch.object(workflow.plan, "actionable", return_value=rows):
            action, reason = workflow.select(self.project, self.policy, new=True)
            self.assertTrue(action.endswith(" draft beta"), action)
            self.assertIn("alpha (private draft)", reason)
            manifest["workspace_id"] = "other"
            path.write_text(json.dumps(manifest))
            action, reason = workflow.select(self.project, self.policy, new=True)
            self.assertTrue(action.endswith(" draft alpha"), action)
            self.assertNotIn("Skipped:", reason)

    def test_new_reports_explicit_exclusion_even_when_nothing_remains(self) -> None:
        import json

        self.types()
        exclude = self.root.parent / "exclude.json"
        exclude.write_text(json.dumps({"schema": 1, "functions": ["alpha", "beta", "gamma"]}))
        action, reason = workflow.select(self.project, self.policy, exclude=exclude, new=True)
        self.assertEqual(action, "No undrafted items.")
        self.assertIn("alpha (explicit exclusion)", reason)
