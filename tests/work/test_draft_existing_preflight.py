"""An existing real draft refuses before heavy prerequisites; the later backend guard still owns races."""

import argparse
import io
from unittest.mock import patch

from tests.project_fixture import ProjectCase
from unbake import steps
from unbake.cli import draft as verb
from unbake.cli.args import Context
from unbake.config import Held
from unbake.layout import split
from unbake.process import named
from unbake.work import draft


class ExistingDraftPreflight(ProjectCase):
    versions = ("us",)

    def context(self, replace=False):
        return Context(
            "draft",
            argparse.Namespace(function="alpha", replace=replace),
            self.project.root,
            None,
            io.StringIO(),
            self.host,
            self.project,
        )

    def file(self):
        path = self.project.work / "alpha/alpha.c"
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def test_existing_payload_does_zero_prerequisite_or_backend_work_and_keeps_bytes(self):
        file = self.file()
        content = b"int alpha(void) { return 1; }\n"
        file.write_bytes(content)
        with (
            patch.object(steps, "ensure", return_value=[]) as prerequisites,
            patch.object(draft, "published_seed", return_value=None),
            patch.object(split, "holding_versions", return_value=("us",)),
            patch.object(draft, "draft", wraps=draft.draft) as backend,
            self.assertRaisesRegex(Held, "draft.exists") as held,
        ):
            verb.run(self.context())
        self.assertEqual(prerequisites.call_count, 0)
        self.assertEqual(backend.call_count, 0)
        self.assertEqual(file.read_bytes(), content)
        self.assertIn("compare", held.exception.next_action)

    def test_replace_reaches_normal_prerequisites_and_backend_without_claiming_success(self):
        self.file().write_text("int alpha(void) { return 1; }\n")
        with (
            patch.object(steps, "ensure", return_value=[]) as prerequisites,
            patch.object(
                draft,
                "draft",
                side_effect=Held(named("fixture.refusal", "test.native_boundary", owner="fixture", stage="draft")),
            ) as backend,
            self.assertRaisesRegex(Held, "test.native_boundary"),
        ):
            verb.run(self.context(replace=True))
        self.assertEqual(prerequisites.call_count, 1)
        self.assertEqual(backend.call_count, 1)

    def test_file_written_during_prerequisites_is_still_refused_by_the_backend(self):
        file = self.file()
        content = b"int alpha(void) { return 2; }\n"

        def prepare(*args, **kwargs):
            file.write_bytes(content)
            return []

        with (
            patch.object(steps, "ensure", prepare),
            patch.object(draft, "published_seed", return_value=None),
            patch.object(split, "holding_versions", return_value=("us",)),
            patch.object(draft, "draft", wraps=draft.draft) as backend,
            self.assertRaisesRegex(Held, "draft.exists"),
        ):
            verb.run(self.context())
        self.assertEqual(backend.call_count, 1)
        self.assertEqual(file.read_bytes(), content)

    def test_replace_rejects_draft_changed_during_prerequisites(self):
        file = self.file()
        file.write_text("int alpha(void) { return 1; }\n")

        def changed(*args, **kwargs):
            file.write_text("int alpha(void) { return 2; }\n")
            return []

        with (
            patch.object(steps, "ensure", side_effect=changed),
            patch.object(draft, "published_seed") as seed,
            self.assertRaisesRegex(Held, "draft.changed"),
        ):
            verb.run(self.context(replace=True))
        self.assertEqual(seed.call_count, 0)
        self.assertEqual(file.read_text(), "int alpha(void) { return 2; }\n")

    def test_replace_cannot_overwrite_new_draft_not_in_prepared_snapshot(self):
        file = self.file()

        def changed(*args, **kwargs):
            file.write_text("int alpha(void) { return 2; }\n")
            return []

        with (
            patch.object(steps, "ensure", side_effect=changed),
            patch.object(draft, "published_seed") as seed,
            self.assertRaisesRegex(Held, "draft.changed"),
        ):
            verb.run(self.context(replace=True))
        self.assertEqual(seed.call_count, 0)
