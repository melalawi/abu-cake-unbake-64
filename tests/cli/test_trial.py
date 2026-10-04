"""Configured public work commands and removed nested routes."""

from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock, patch

from tests.cli.support import MainCase
from unbake.decomp import type_context, work


class TrialTests(MainCase):
    def setUp(self) -> None:
        super().setUp()
        self.guidance = patch("unbake.cli.guidance.resolve", return_value="unbake next")
        self.guidance.start()
        self.addCleanup(self.guidance.stop)
        context = patch.object(type_context, "required", return_value=("d" * 64, ""))
        context.start()
        self.addCleanup(context.stop)
        cleared = patch.object(type_context, "clear_redraft")
        cleared.start()
        self.addCleanup(cleared.stop)

    def test_try_uses_declared_work_and_all_holding_versions(self) -> None:
        operation = Mock(return_value=SimpleNamespace(function="alpha", source_sha256="a" * 64))
        code, out, error = self.run_main(
            self.args("try", str(self.source)), {"trial": self.module("trial", retain_draft=operation)}
        )
        self.assertEqual(code, 0, error)
        operation.assert_called_once_with(
            self.project, self.policy, self.source, self.scratch, versions=None, flags=False, overlay_root=None
        )
        self.assertIn("retained alpha source_sha256", out)

    def test_try_without_scratch_uses_valid_external_default(self) -> None:
        operation = Mock(return_value=SimpleNamespace(function="alpha", source_sha256="a" * 64))
        args = ["--project", str(self.root), "try", str(self.source)]
        code, out, error = self.run_main(args, {"trial": self.module("trial", retain_draft=operation)})
        self.assertEqual(code, 0, error + out)
        scratch = operation.call_args.args[3]
        self.assertFalse(scratch.resolve().is_relative_to(self.project.root))

    def test_volatile_storage_is_delegated_to_the_proven_fold(self) -> None:
        from unbake.project.config import Held

        self.source.write_text(' #include "../legacy/scalars.h"\nvoid alpha(void) { volatile int local; local = 1; }\n')
        operation = Mock(side_effect=Held("volatile", "volatile-storage: volatile int local; rewrite changes codegen"))
        code, out, error = self.run_main(
            self.args("try", str(self.source)), {"trial": self.module("trial", retain_draft=operation)}
        )
        self.assertEqual(code, 1, error)
        operation.assert_called_once()
        self.assertIn("volatile int local", out)
        self.assertIn("rewrite changes codegen", out)
        self.assertNotIn("trial.source_rules", out)

    def test_draft_uses_configured_naming_version_and_stages_headers(self) -> None:
        from unbake.cli import draft

        directory = self.project.work / "generated"
        directory.mkdir(parents=True)
        source = directory / "alpha.c"
        source.write_bytes(self.source.read_bytes())
        work.overlay(self.project, directory)
        generation = self.project.build / "us-rev1.1"
        operation = Mock(return_value=source)
        manifest = {"subject": "alpha"}
        with (
            patch.object(draft, "owning_versions", return_value=list(self.project.versions)),
            patch.object(draft, "inputs", return_value=nullcontext({self.project.versions[0]: (generation, source)})),
            patch.object(work, "identity", return_value=manifest),
            patch.object(work, "persist") as persisted,
        ):
            code, out, error = self.run_main(self.args("draft", "alpha"), {"m2c": self.module("m2c", draft=operation)})
        self.assertEqual(code, 0, error)
        operation.assert_called_once_with(
            operation.call_args.args[0],
            self.policy,
            "alpha",
            self.project.versions[0],
            self.policy.state_root / "draft-work",
            generation=generation,
            type_context="",
            announce=False,
            use_type_db=True,
        )
        persisted.assert_not_called()
        self.assertTrue((self.policy.state_root / "draft-work/drafts/alpha/alpha.c").is_file())
        self.assertIn(str(self.policy.state_root / "draft-work/drafts/alpha/alpha.c"), out)

    def test_draft_accepts_item_absent_from_naming_version(self) -> None:
        from unbake.cli import draft

        containing = self.project.versions[-1]
        directory = self.project.work / "regional"
        directory.mkdir(parents=True)
        source = directory / "alpha.c"
        source.write_bytes(self.source.read_bytes())
        work.overlay(self.project, directory)
        operation = Mock(return_value=source)
        generation = self.project.build / (containing + ".1")
        with (
            patch.object(draft, "owning_versions", return_value=[containing]),
            patch.object(draft, "inputs", return_value=nullcontext({containing: (generation, source)})),
            patch.object(work, "identity", return_value={"subject": "alpha"}),
            patch.object(work, "persist"),
        ):
            code, out, error = self.run_main(self.args("draft", "alpha"), {"m2c": self.module("m2c", draft=operation)})
        self.assertEqual(code, 0, error)
        self.assertEqual(operation.call_args.args[3], containing)
        self.assertIn("draft version: " + containing, out)

    def test_raw_graphics_copy_is_delegated_to_proven_sdk_recovery(self) -> None:
        self.source.write_text("void alpha(void) { p->words.w0 = 0xE7000000; }\n")
        from unbake.project.config import Held

        operation = Mock(side_effect=Held("gbi", "SDK macro recovery: first unmatched write raw-gfx:1"))
        code, out, error = self.run_main(
            self.args("try", str(self.source)), {"trial": self.module("trial", retain_draft=operation)}
        )
        self.assertEqual(code, 1, error)
        self.assertIn("SDK macro recovery: first unmatched write raw-gfx:1", out)
        operation.assert_called_once()

    def test_retired_nested_routes_and_scratch_flags_refuse(self) -> None:
        for operands in (
            ("decomp", "try", "alpha.c"),
            ("decomp", "draft", "alpha"),
            ("try", "alpha.c", "--scratch", "scratch"),
            ("draft", "alpha", "--version", "us"),
        ):
            with self.subTest(operands=operands):
                code, _, _ = self.run_main(self.args(*operands))
                self.assertEqual(code, 1)

    def test_struct_work_is_named_unfinished(self) -> None:
        for operands, key in (
            (("draft", "--struct", "record"), "draft.struct"),
            (("try", "layout.h"), "trial.struct"),
            (("submit", "layout.h"), "submit.struct"),
        ):
            with self.subTest(operands=operands):
                code, out, _ = self.run_main(self.args(*operands))
                self.assertEqual(code, 1)
                self.assertIn(key, out)
