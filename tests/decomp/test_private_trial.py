"""Private trial preparation and snapshot reads do not publish project inputs."""

import io
import tempfile
import unittest
from contextlib import nullcontext, redirect_stdout
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from unbake.decomp import trial, trial_data, trial_target, type_context, work
from unbake.decomp.needs import SymbolNeed
from unbake.decomp.trial_compile import scratch_directory
from unbake.project import makefile
from unbake.project.config import Held, Project


@dataclass
class OutputPolicy:
    state_root: Path
    cache_root: Path


class PrivateTrialTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        root = self.root / "project"
        root.mkdir()
        self.project = Project(
            root=root,
            name="game",
            title="game",
            names_from="us",
            versions=("us",),
            src=root / "src",
            include=(root / "include",),
            asm=root / "asm",
            tools=root / "tools",
            compilers={},
            default_compiler="cc",
            units={},
            version_map={},
            id="id",
            workspace_id="workspace",
            roms=root / "roms",
            build=root / "build",
            work=root / "build/work",
            drafts=root / "build/drafts",
        )

    def test_scratch_refuses_every_project_subtree(self):
        with self.assertRaisesRegex(Held, "trial.scratch"):
            scratch_directory(self.project, self.project.work, "try")
        self.assertFalse(self.project.build.exists())
        external = self.root / "scratch"
        self.assertEqual(scratch_directory(self.project, external, "try"), external)

    def test_missing_generation_never_builds_in_read_only_mode(self):
        with (
            patch("unbake.decomp.trial_target.make_target") as make,
            patch.object(Project, "version"),
            self.assertRaisesRegex(Held, "trial.target_missing"),
            trial_target._target_generation(self.project, "us", read_only=True),
        ):
            self.fail("missing generation admitted")
        make.assert_not_called()
        self.assertFalse(self.project.build.exists())

    def test_retention_writes_only_scratch_and_preserves_authored_identity(self):
        source = self.root / "alpha.c"
        source.write_text("void alpha(void) {}\n")
        scratch = self.root / "scratch"
        policy = OutputPolicy(self.root / "state", self.root / "cache")
        result = trial.Trial("alpha", "prepared", {}, [], "submit " + str(source))
        with (
            patch.object(work, "trial_view", return_value=source),
            patch.object(work, "identity", return_value={}),
            patch.object(work, "persist") as persist,
            patch.object(trial, "owning_versions", return_value=["us"]),
            patch.object(trial.entry_layout, "owners", return_value=[object()]),
            patch.object(trial, "trial_inputs", return_value=nullcontext({"us": (self.root, source)})),
            patch.object(trial, "try_draft", return_value=result),
            patch("unbake.decomp.trial_compilers.resolve", return_value=(self.project, {}, result)),
            patch.object(trial, "store_trial") as store,
            redirect_stdout(io.StringIO()),
        ):
            trial.retain_draft(self.project, policy, source, scratch, None)
        persist.assert_not_called()
        self.assertEqual(store.call_args.args[1].state_root, scratch / "state")
        self.assertEqual(result.source_sha256, work.digest(source.read_bytes()))
        self.assertTrue(list(scratch.glob("*/manifest.json")))
        self.assertFalse(self.project.build.exists())

    def test_private_fold_materializes_overlay_context_as_well_as_fold_headers(self):
        from unbake.match.declarations import Folded

        project = replace(self.project, versions=())
        for root in (*project.include, project.root / "versions", project.tools, project.roms):
            root.mkdir(parents=True)
        (project.root / "config.toml").write_text("schema = 1")
        (project.include[0] / "types.h").write_text("typedef int s32;")
        source = self.root / "alpha.c"
        source.write_text("void alpha(void) {}")

        def fold(staged, policy, headers, candidate, changes):
            # Existing overlay headers are adopted by the shared context, and
            # need not appear among this fold's newly proposed header edits.
            headers.texts[staged.include[0] / "proposal.h"] = "typedef int Proposed;"
            return Folded("alpha", candidate.content.decode(), [], {})

        with (
            patch("unbake.match.batch_fold._fold_one", side_effect=fold),
            patch.object(work.split, "holding_versions", return_value=()),
        ):
            prepared = work.trial_view(project, None, source, self.root / "scratch")
        self.assertEqual((prepared.parent.parent / "include/proposal.h").read_text(), "typedef int Proposed;")
        self.assertEqual(prepared.read_bytes(), source.read_bytes())
        self.assertFalse((project.include[0] / "proposal.h").exists())

    def test_data_inference_uses_submit_proof_and_conflict_validation(self):
        pending = [SymbolNeed("us", "table_identity", 0x80001000, 0, "data", "address", 0, "ROM pairs")]
        with (
            patch.object(trial_data.data_symbols, "needs", return_value=pending) as needs,
            patch.object(trial_data.data_symbols, "resolve", return_value=[]) as resolve,
        ):
            result = trial_data.infer(self.project, None, "alpha", "us", self.root / "alpha.o")
        self.assertEqual(result, {"table_identity": 0x80001000})
        needs.assert_called_once()
        resolve.assert_called_once_with(pending, self.project, None)
        self.assertFalse(self.project.build.exists())

    def test_address_shaped_name_cannot_hide_another_placement(self):
        pending = [SymbolNeed("us", "D_80002000", 0x80001000, 0, "data", "address", 0, "ROM pairs")]
        with (
            patch.object(trial_data.data_symbols, "needs", return_value=pending),
            self.assertRaisesRegex(Held, "address-shaped name"),
        ):
            trial_data.infer(self.project, None, "alpha", "us", self.root / "alpha.o")

    def test_explicit_overlay_paths_are_allowed_only_under_named_roots(self):
        root = self.root / "overlay"
        project = SimpleNamespace(root=self.project.root, overlay_roots=(root,))
        self.assertEqual(makefile.relative(project, root / "shared"), str(root / "shared"))
        with self.assertRaisesRegex(Held, "explicit overlay root"):
            makefile.relative(project, self.root / "other")

    def test_stale_snapshot_names_staleness_without_solving(self):
        database = self.project.build / "types/database.json"
        database.parent.mkdir(parents=True)
        database.write_text("{}")
        api = SimpleNamespace(load=unittest.mock.Mock(), context=unittest.mock.Mock(return_value="solved context"))
        with (
            patch.object(type_context, "provider", return_value=api),
            patch.object(type_context, "required", side_effect=Held("draft", "types.inputs_stale: inputs changed")),
            redirect_stdout(io.StringIO()) as output,
        ):
            digest, context = type_context.snapshot(self.project, "alpha")
        self.assertEqual(digest, work.digest(b"{}"))
        self.assertEqual(context, "solved context")
        self.assertIn("snapshot: stale", output.getvalue())
        api.load.assert_called_once_with(self.project, allow_stale=True)
        api.context.assert_called_once_with(self.project, function="alpha", allow_stale=True)
