"""Final publication validation fails closed before literal-SHA transport."""

from unittest.mock import patch

from tests.project_fixture import ProjectCase
from unbake import build
from unbake.config import Held
from unbake.process import Fault, named
from unbake.project import publication_push


class FinalCommitCheckTests(ProjectCase):
    def push(self, outcome, mutation=None):
        calls = []
        head, dirty, untracked = "final-sha", "", ""

        def git(project, *args):
            calls.append(args)
            if args in (("rev-parse", "HEAD"), ("rev-parse", "FETCH_HEAD")):
                return head
            if args[0] == "merge-base":
                return head
            if args[0] == "status":
                return dirty
            if args[0] == "ls-files":
                return untracked
            return ""

        def check(project, host, *, files_only):
            nonlocal head, dirty, untracked
            self.assertFalse(files_only)
            calls.append(("check", head))
            if mutation == "head":
                head = "changed-sha"
            elif mutation == "tracked":
                dirty = " M src/alpha.c"
            elif mutation == "untracked":
                untracked = "new-output"
            return outcome

        with patch.object(publication_push, "_git", side_effect=git), patch.object(build, "check", side_effect=check):
            try:
                result = publication_push.push(self.project, self.host, "origin")
            except Held as error:
                result = error
        return result, calls

    def test_success_checks_full_build_and_transports_literal_validated_sha(self):
        result, calls = self.push(build.Outcome(True, True))
        self.assertEqual(result["head"], "final-sha")
        self.assertEqual(calls.count(("check", "final-sha")), 1)
        self.assertEqual(calls[-1], ("push", "--", "origin", "final-sha:refs/heads/main"))
        self.assertTrue(result["check"]["built"])

    def test_failed_or_unbuilt_check_never_pushes(self):
        for outcome in (build.Outcome(False, False), build.Outcome(True, False)):
            with self.subTest(outcome=outcome):
                result, calls = self.push(outcome)
                self.assertIsInstance(result, Held)
                self.assertFalse(any(call[0] == "push" for call in calls))

    def test_typed_check_fault_remains_the_first_cause(self):
        fault = Fault(named("check.source_rules", "source refused", owner="steps", stage="preflight"))
        result, calls = self.push(build.Outcome(False, False, fault=fault.document()))
        self.assertEqual(result.key, "check.source_rules")
        self.assertEqual(result.fault.cause.owner, "steps")
        self.assertEqual(result.data["head"], "final-sha")
        self.assertFalse(any(call[0] == "push" for call in calls))

    def test_check_changes_to_head_tracked_or_untracked_files_refuse_transport(self):
        for mutation in ("head", "tracked", "untracked"):
            with self.subTest(mutation=mutation):
                result, calls = self.push(build.Outcome(True, True), mutation)
                self.assertEqual(result.key, "publish.check_changed")
                self.assertFalse(any(call[0] == "push" for call in calls))
