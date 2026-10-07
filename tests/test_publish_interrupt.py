"""An interrupted publication has no final cause and preserves ready retry work."""

import argparse
import io
import json
from contextlib import nullcontext, redirect_stderr, redirect_stdout
from unittest.mock import patch

from tests.project_fixture import ProjectCase
from unbake import land
from unbake.cli import main, publish
from unbake.cli.args import Context


class PublishInterruptTests(ProjectCase):
    def context(self, **options):
        files = [self.project.work / name / f"{name}.c" for name in ("alpha", "beta")]
        for file in files:
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text(f"int {file.stem}(void) {{return 1;}}\n")
        args = argparse.Namespace(files=files, original=[], require_version=None, events=False, fuzzy=False, **options)
        return Context("publish", args, self.project.root, None, io.StringIO(), self.host)

    def test_interrupt_is_retryable_then_each_ready_item_runs_once(self):
        context = self.context()
        with (
            patch.object(land, "land", side_effect=[KeyboardInterrupt(), "alpha-commit", "beta-commit"]) as native,
            patch.object(land.steps, "ensure", return_value=[]) as maintenance,
        ):
            interrupted = publish.run(context)
            self.assertEqual((interrupted.status, interrupted.key), ("interrupted", None))
            self.assertEqual(interrupted.data["failed"], {})
            self.assertEqual(interrupted.data["ready"], ["alpha", "beta"])
            self.assertTrue(interrupted.data["retryable"])
            self.assertEqual(native.call_count, 1)
            maintenance.assert_not_called()
            retried = publish.run(context)
        self.assertEqual(retried.status, "ok")
        self.assertEqual(retried.data["landed"], ["alpha", "beta"])
        self.assertEqual(native.call_count, 3)
        self.assertEqual(maintenance.call_count, 2)

    def test_successful_prefix_does_not_appear_in_the_ready_retry_command(self):
        context = self.context()
        with (
            patch.object(land, "land", side_effect=["accepted", KeyboardInterrupt()]) as native,
            patch.object(land.steps, "ensure", return_value=[]),
        ):
            result = publish.run(context)
        self.assertEqual(native.call_count, 2)
        self.assertEqual(result.data["commits"], ["accepted"])
        self.assertEqual(result.data["ready"], ["beta"])
        self.assertEqual(result.data["failed"], {})
        self.assertNotIn("alpha.c", result.next)
        self.assertIn("beta.c", result.next)
        self.assertEqual(result.status, "interrupted")

    def test_a_receipted_commit_is_preserved_if_its_cleanup_is_interrupted(self):
        context = self.context()

        def accepted(project, host, file, on_commit=None, **kwargs):
            on_commit({"function": "alpha", "commit": "accepted", "proof": {"versions": ["us", "eu"]}})
            raise KeyboardInterrupt

        with (
            patch.object(land, "land", side_effect=accepted) as native,
            patch.object(land.steps, "ensure") as maintenance,
        ):
            result = publish.run(context)
        self.assertEqual(native.call_count, 1)
        maintenance.assert_not_called()
        self.assertEqual(result.data["landed"], ["alpha"])
        self.assertEqual(result.data["ready"], ["beta"])
        self.assertEqual(result.data["versions"], {"alpha": ["us", "eu"]})
        self.assertEqual(result.data["failed"], {})

    def test_interrupted_maintenance_keeps_the_committed_prefix_and_ready_suffix(self):
        context = self.context()
        with (
            patch.object(land, "land", return_value="accepted") as native,
            patch.object(land.steps, "ensure", side_effect=KeyboardInterrupt()) as maintenance,
        ):
            result = publish.run(context)
        self.assertEqual((native.call_count, maintenance.call_count), (1, 1))
        self.assertEqual(result.data["commits"], ["accepted"])
        self.assertEqual(result.data["ready"], ["beta"])
        self.assertNotIn("post_commit_failure", result.data)
        self.assertEqual((result.status, result.key), ("interrupted", None))

    def test_main_emits_exit_130_without_a_held_cause(self):
        out, err = io.StringIO(), io.StringIO()
        with (
            patch.object(main, "_root", return_value=None),
            patch.object(main.config, "load_host", return_value=self.host),
            patch.object(main.admission, "command", return_value=nullcontext()),
            patch.object(publish, "run", side_effect=KeyboardInterrupt()),
            redirect_stdout(out),
            redirect_stderr(err),
        ):
            code = main.main(["publish", "alpha.c"])
        result = json.loads(out.getvalue())
        self.assertEqual((code, result["status"], result["key"]), (130, "interrupted", None))
        self.assertTrue(result["data"]["retryable"])
        self.assertNotIn("fault", result["data"])
        self.assertNotIn("HELD(", err.getvalue())
        self.assertIn("alpha.c", result["next"])

    def test_publication_retains_no_native_receipt_payload_matrix(self):
        import weakref

        context = self.context()
        references = []

        class Inputs(dict):
            pass

        def accepted(project, host, file, on_commit=None, **kwargs):
            inputs = Inputs({"source": file.read_text()})
            references.append(weakref.ref(inputs))
            on_commit(
                {
                    "function": file.stem,
                    "commit": "accepted-" + file.stem,
                    "proof": {"versions": ["us", "eu"], "files": inputs},
                }
            )
            return "accepted-" + file.stem

        with (
            patch.object(land, "land", side_effect=accepted) as native,
            patch.object(land.steps, "ensure", return_value=[]),
        ):
            result = publish.run(context)
        self.assertEqual(native.call_count, 2)
        self.assertEqual(result.data["landed"], ["alpha", "beta"])
        self.assertEqual(sum(reference() is not None for reference in references), 0)
