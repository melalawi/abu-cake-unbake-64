"""One reasoned action from current type, draft and trial evidence."""

from unittest.mock import patch

from tests.match.support import MatchFixture
from unbake.cli import workflow
from unbake.decomp import work


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
        self.assertEqual(self.queued(), [])
        self.assert_untouched()

    def test_changed_types_request_redraft_before_old_exact_submission(self) -> None:
        self.types()
        source = self.editable()
        self.prove(source)
        with patch.object(workflow.type_context, "redrafts", return_value={"alpha": {"reasons": ["callee type"]}}):
            action, reason = workflow.select(self.project, self.policy)
        self.assertTrue(action.endswith(" draft alpha"), action)
        self.assertIn("redraft required", reason)
        self.assertEqual(self.queued(), [])
