from unittest.mock import Mock

from tests.cli.support import MainCase


class TrialTests(MainCase):
    def test_try_scores_and_retains_only_its_own_scratch_artifacts(self) -> None:
        modules, result, captured, _target, fuzzy, store = self.trial_modules()
        stale = self.scratch / "old/us"
        stale.mkdir(parents=True)
        (stale / "trial.elf").write_bytes(b"stale")
        code, out, error = self.run_main(
            self.args("decomp", "try", str(self.source), "--scratch", str(self.scratch), "--version", "us"), modules
        )
        self.assertEqual(code, 0, error)
        self.assertEqual(captured["versions"], ["us"])
        self.assertTrue(captured["scratch"].is_relative_to(self.scratch))
        directory = captured["scratch"] / "alpha.unique/us"
        fuzzy.assert_called_once_with(
            self.project, self.policy, "us", "alpha", directory / "baserom.score.o", directory / "draft.score.o"
        )
        store.add.assert_called_once_with(result, self.source, {"us": 88.5})
        self.assertIn("retained NON_MATCHING draft alpha", out)

    def test_draft_runs_m2c_then_tries_all_holding_versions(self) -> None:
        modules, _result, captured, _target, _fuzzy, _store = self.trial_modules()
        draft = Mock(return_value=self.source)
        modules["m2c"] = self.module("m2c", draft=draft)
        code, _out, error = self.run_main(
            self.args("decomp", "draft", "alpha", "--version", "us", "--scratch", str(self.scratch)), modules
        )
        draft.assert_called_once_with(self.project, self.policy, "alpha", "us", self.scratch)
        self.assertEqual(captured["versions"], None)
        self.assertEqual(code, 0, error)

    def test_try_refuses_changed_generation_before_scoring(self) -> None:
        modules, _result, _captured, _target, fuzzy, store = self.trial_modules(change_generation=True)
        code, _out, error = self.run_main(
            self.args("decomp", "try", str(self.source), "--scratch", str(self.scratch)), modules
        )
        self.assertEqual(code, 1)
        self.assertIn("build/us: generation changed", error)
        fuzzy.assert_not_called()
        store.add.assert_not_called()

    def test_try_refuses_missing_elf_by_version_and_path(self) -> None:
        modules, _result, captured, _target, _fuzzy, store = self.trial_modules(omit_artifact=True)
        code, _out, error = self.run_main(
            self.args("decomp", "try", str(self.source), "--scratch", str(self.scratch)), modules
        )
        self.assertEqual(code, 1)
        self.assertIn("VERSION us draft ELF", error)
        self.assertIn(str(captured["scratch"]), error)
        store.add.assert_not_called()

    def test_try_refuses_project_scratch_before_calling_trial(self) -> None:
        operation = Mock()
        code, _out, error = self.run_main(
            self.args("decomp", "try", str(self.source), "--scratch", str(self.root / "scratch")),
            {"trial": self.module("trial", try_draft=operation)},
        )
        self.assertEqual(code, 1)
        self.assertIn("scratch", error)
        self.assertIn("project.root", error)
        self.assertFalse((self.root / "scratch").exists())
        operation.assert_not_called()
