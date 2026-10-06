"""Commit receipts are observable before optional merging or a later refused sibling."""

import argparse
import io
import json
from unittest.mock import patch

from tests.project_fixture import ProjectCase
from unbake import land
from unbake.cli import publish
from unbake.cli.args import Context
from unbake.config import Held
from unbake.fold.apply import Folded


class PublishEventTests(ProjectCase):
    def test_first_commit_is_flushed_before_merge_and_failed_sibling(self):
        self.publish_case(merge_refusal=False)

    def test_merge_refusal_cannot_hide_or_relabel_an_already_accepted_commit(self):
        self.publish_case(merge_refusal=True)

    def publish_case(self, *, merge_refusal):
        alpha = self.root / "alpha.c"
        beta = self.root / "beta.c"
        alpha.write_text("int alpha(void) { return 1; }\n")
        beta.write_text("int beta(void) { return 2; }\n")
        stream = io.StringIO()
        args = argparse.Namespace(files=[alpha, beta], original=[], require_version=None, events=True, fuzzy=False)
        context = Context("publish", args, self.project.root, None, stream, self.host)
        observed = []

        def fold(project, host, function, text, **kwargs):
            if function == "beta":
                self.assertTrue(stream.getvalue(), "successful sibling receipt was buffered")
                observed.append("beta")
                raise Held("fold", "fold.refused: beta lacks a canonical declaration")
            return Folded(function, text, {}, ())

        def merge(*args, **kwargs):
            event = json.loads(stream.getvalue().splitlines()[0])
            self.assertEqual(
                (event["event"], event["function"], event["commit"]), ("fn.committed", "alpha", "accepted-alpha")
            )
            observed.append("merge")
            if merge_refusal:
                raise Held("merge", "merge.proof: a subsequent merge does not prove")

        with (
            patch.object(
                land, "exact_attempt", return_value=type("A", (), {"compiler": "ido-7.1", "sha256": "a" * 64})()
            ),
            patch("unbake.fold.apply.fold", side_effect=fold),
            patch.object(land, "prove", return_value=land.Proof(["us", "eu"], {self.project.include[-1] / "types.h"})),
            patch.object(land, "_commit"),
            patch.object(land, "_git", return_value="accepted-alpha\n"),
            patch.object(land.steps, "ensure", side_effect=merge),
            patch.object(land.steps, "record"),
            patch.object(land.buildfiles, "write", return_value=[]),
            patch("unbake.report.progress.write", return_value=[]),
        ):
            result = publish.run(context)
        self.assertEqual(observed, ["merge"] if merge_refusal else ["merge", "beta"])
        self.assertEqual(result.status, "held")
        self.assertEqual(result.data["landed"], ["alpha"])
        self.assertEqual(result.data["commits"], ["accepted-alpha"])
        if merge_refusal:
            self.assertEqual(result.data["post_commit_failure"]["key"], "merge.proof")
            self.assertEqual(result.data["failed"], {})
        event = json.loads(stream.getvalue())
        self.assertEqual(event["proof"]["versions"], ["us", "eu"])
        self.assertIn("include/types.h", event["proof"]["files"])
        self.assertIn("src/alpha.c", event["proof"]["files"])
