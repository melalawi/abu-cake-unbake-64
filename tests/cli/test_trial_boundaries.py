"""Compiled definition checks and method discovery through the flow dispatcher."""

import shutil
from types import SimpleNamespace
from unittest.mock import patch

from tests.cli.support import MainCase
from tests.decomp.support import assemble, assembly, fixture
from unbake.decomp import drafts, trial
from unbake.decomp.trial_compare import TYPES, Compare
from unbake.search import available, methods


class TrialBoundaryTests(MainCase):
    def test_try_requires_compiled_definition_matching_the_item_name(self) -> None:
        directory = self.directory / "objects"
        directory.mkdir()
        self.project, _, source = fixture(directory, case=self)
        wrong = assemble(directory, "wrong", assembly("other", [0x24020001, 0x03E00008, 0]))

        def compile_source(project, policy, copied, version, output):
            shutil.copyfile(wrong, output)
            return output

        with patch("unbake.project.build.compile_object", side_effect=compile_source):
            code, out, error = self.run_main(self.args("try", str(source)))
        self.assertEqual(code, 1)
        self.assertIn("function alpha: compiled definition missing", out + error)

    def test_search_retains_guarded_identity_before_mutation_budget_refusal(self) -> None:
        from tests.match.support import MatchFixture

        project_fixture = MatchFixture("runTest")
        project_fixture.setUp()
        self.addCleanup(project_fixture.doCleanups)
        self.project, self.policy, self.root = project_fixture.project, project_fixture.policy, project_fixture.root
        self.source = project_fixture.draft("alpha", "#ifdef NON_MATCHING\nint alpha(void) { return 1; }\n#endif\n")
        result = trial.Trial(
            "alpha",
            drafts.source_identity(self.source.read_bytes()),
            {"us": Compare("us", 2, 2, dict.fromkeys(TYPES, 0), [], 100, ())},
            [],
            "try again",
        )
        with (
            patch.object(trial, "try_draft", return_value=result),
            patch("unbake.search.core.preprocess", return_value=self.source.read_text()),
            patch("unbake.decomp.explain.allocation", return_value=SimpleNamespace(differences=[], pseudos=[])),
        ):
            code, out, error = self.run_main(
                self.args(
                    "decomp",
                    "search",
                    str(self.source),
                    "--method",
                    "order",
                    "--out",
                    str(self.scratch),
                    "--budget-seconds",
                    "1",
                ),
            )
        self.assertEqual(code, 1)
        self.assertIn("zero mutations evaluated", out + error)
        record = drafts.Store(self.policy, self.project).rows("alpha")[-1]
        self.assertNotEqual(record["sha256"], record["source_sha256"])
        self.assertTrue(record["identical_everywhere"])

    def test_method_list_uses_generator_registry_without_trial_inputs(self) -> None:
        with patch("unbake.search.core.run") as run:
            code, out, error = self.run_main(self.args("decomp", "search", "--method", "list"))
            self.assertEqual(code, 0, error)
            self.assertEqual(out.splitlines()[:-1], [f"OK(search): {name}" for name in available()])
            run.assert_not_called()
        for name in available():
            if name != "permute":
                self.assertTrue(callable(methods(name)[0].propose))

    def test_schedule_explanation_dispatches_selected_source_and_version(self) -> None:
        from dataclasses import dataclass

        @dataclass
        class Evidence:
            version: str

        with patch("unbake.decomp.explain.order", return_value=Evidence("us")) as explain:
            code, out, error = self.run_main(
                self.args("decomp", "explain", "order", str(self.source), "--version", "us")
            )
        self.assertEqual(code, 0, out + error)
        explain.assert_called_once_with(self.project, self.policy, self.source, "us")
