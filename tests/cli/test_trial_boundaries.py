"""Draft selection and method discovery through the command dispatcher."""

import shutil
from types import SimpleNamespace
from unittest.mock import patch

from tests.cli.support import MainCase
from tests.decomp.support import assemble, assembly, fixture
from unbake.decomp import drafts, trial
from unbake.decomp.trial_compare import TYPES, Compare
from unbake.search import available, methods


class TrialBoundaryTests(MainCase):
    def test_explicit_function_requires_a_compiled_definition_by_name(self) -> None:
        directory = self.directory / "objects"
        directory.mkdir()
        self.project, _, source = fixture(directory)
        wrong = assemble(directory, "wrong", assembly("other", [0x24020001, 0x03E00008, 0]))

        def compile_source(project, policy, copied, version, output):
            shutil.copyfile(wrong, output)
            return output

        with patch("unbake.project.build.compile_object", side_effect=compile_source):
            code, _, error = self.run_main(
                self.args("decomp", "try", str(source), "--function", "alpha", "--scratch", str(self.scratch))
            )
        self.assertEqual(code, 1)
        self.assertIn("function alpha: compiled definition missing", error)

    def test_search_retains_guarded_identity_before_mutation_budget_refusal(self) -> None:
        self.source.write_text("#ifdef NON_MATCHING\nint alpha(void) { return 1; }\n#endif\n")
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
            code, _, error = self.run_main(
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
                )
            )
        self.assertEqual(code, 1)
        self.assertIn("zero mutations evaluated", error)
        record = drafts.Store(self.policy, self.project).rows("alpha")[-1]
        self.assertNotEqual(record["sha256"], record["source_sha256"])
        self.assertTrue(record["identical_everywhere"])

    def test_try_infers_definition_from_arbitrary_path_and_retains_function(self) -> None:
        modules, result, captured, _, _, store = self.trial_modules()
        renamed = self.directory / "fuzzy-base.input"
        renamed.write_bytes(self.source.read_bytes())
        code, _, error = self.run_main(
            self.args("decomp", "try", str(renamed), "--scratch", str(self.scratch)), modules
        )
        self.assertEqual(code, 0, error)
        self.assertEqual(captured["function"], "alpha")
        store.add.assert_called_once_with(result, renamed, {"us": 88.5})

    def test_try_refuses_multiple_names_and_accepts_explicit_definition(self) -> None:
        modules, _, captured, _, _, _ = self.trial_modules()
        self.source.write_text("/* int fake(void) {} */\nint alpha(void) { return 1; }\nvoid helper(void) {}\n")
        operands = self.args("decomp", "try", str(self.source), "--scratch", str(self.scratch))
        code, _, error = self.run_main(operands, modules)
        self.assertEqual(code, 1)
        self.assertIn("definitions: alpha, helper", error)
        code, _, error = self.run_main([*operands, "--function", "alpha"], modules)
        self.assertEqual(code, 0, error)
        self.assertEqual(captured["function"], "alpha")
        code, _, error = self.run_main([*operands, "--function", "missing"], modules)
        self.assertEqual(code, 1)
        self.assertIn("missing: no owning text row", error)

    def test_method_list_uses_generator_registry_without_trial_inputs(self) -> None:
        with patch("unbake.search.core.run") as run:
            code, out, error = self.run_main(self.args("decomp", "search", "--method", "list"))
            self.assertEqual(code, 0, error)
            self.assertEqual(out.splitlines(), [f"OK(search): {name}" for name in available()])
            run.assert_not_called()
        for name in available():
            if name != "permute":
                self.assertTrue(callable(methods(name)[0].propose))
