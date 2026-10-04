"""Recovery actions belong to refusals; large receipts keep their next line visible."""

import json
import shlex
from contextlib import nullcontext
from unittest.mock import patch

from tests.cli.support import MainCase
from unbake.cli import draft, guidance
from unbake.decomp import m2c, work
from unbake.config import Held


class NextLinesTests(MainCase):
    real_guidance = True

    def action(self, output):
        return shlex.split(output.splitlines()[-1].removeprefix("Next: "))

    def test_refusal_action_overrides_reason_and_keeps_project_policy(self):
        policy = self.directory / "operator policy.toml"
        error = Held("try", "trial.overlay_stale: changed", next_action="unbake draft alpha")
        with patch.dict("os.environ"), patch("unbake.cli.setup.run", side_effect=error):
            code, out, stderr = self.run_main(["--project", str(self.root), "--policy", str(policy), "setup"])
        self.assertEqual((code, stderr), (1, ""))
        self.assertEqual(
            self.action(out), ["unbake", "--project", str(self.root), "--policy", str(policy), "draft", "alpha"]
        )
        self.assertNotIn("Supply", out)

    def test_all_generic_fallbacks_hide_internal_keys(self):
        for key in ("trial.overlay_root", "layout.plan", "policy.splat", "draft.struct", "interrupted operation"):
            with self.subTest(key=key):
                action = guidance.resolve(self.root, missing=key, retry="unbake setup")
                self.assertNotIn(key, action)
                self.assertNotIn("Supply", action)
                self.assertTrue(action.endswith("Then run unbake setup."))
        with patch("unbake.config.load_pending", side_effect=Held("config", "project.state: broken")):
            self.assertNotIn("project.state", guidance.resolve(self.root))
        for error in (Held("next", "next.project: blocked"), ValueError("database.internal: broken")):
            with patch("unbake.cli.workflow.select", side_effect=error):
                action = guidance.resolve(self.root)
                self.assertNotIn("Supply", action)
                self.assertNotIn("next.project", action)
                self.assertNotIn("database.internal", action)
        with patch("unbake.cli.workflow.select", side_effect=Held("next", "key", next_action="unbake solve")):
            self.assertEqual(guidance.resolve(self.root), "unbake solve")
        with patch("unbake.config.load_pending", side_effect=Held("config", "key", next_action="unbake setup")):
            self.assertEqual(guidance.resolve(self.root), "unbake setup")

    def test_solve_caps_sample_and_rewrites_complete_list_even_when_empty(self):
        for count in (2199, 5, 2, 0):
            with self.subTest(count=count):
                rows = [{"key": f"types.conflict:{i}", "alternatives": ["int", "float"]} for i in range(count)]
                if rows:
                    rows[-1] = {"key": f"types.conflict:{count - 1}", "reason": "incompatible layout"}
                value = dict(
                    revision=7, unknown=["unknown"], conflicts=rows, functions={}, globals={}, structs={}, arrays={}
                )
                with (
                    patch("unbake.cli.solve.solve", return_value=value),
                    patch("unbake.cli.solve.redrafts", return_value=[]),
                ):
                    code, out, error = self.run_main(self.args("solve"))
                self.assertEqual((code, error), (0, ""))
                path = self.project.build / "types/conflicts.txt"
                expected = [f"{row['key']}: {row.get('alternatives', row.get('reason'))}" for row in rows]
                self.assertEqual(path.read_text().splitlines(), expected)
                samples = [line for line in out.splitlines() if line.startswith("OK(solve): types.conflict:")]
                self.assertEqual(samples, ["OK(solve): " + line for line in expected[:5]])
                self.assertIn(f"conflicts={count}", out)
                self.assertIn(f"conflict list: {path}; showing {min(count, 5)} of {count}", out)
                self.assertLessEqual(len(out.splitlines()), 13)
                self.assertEqual(self.action(out)[-1], "next")

    def stale_source(self):
        directory = self.scratch / "drafts/alpha"
        directory.mkdir(parents=True)
        source = directory / "alpha.c"
        header = self.project.include[0] / "recovery.h"
        header.write_text("typedef int Recovery;\n")
        source.write_text('#include "recovery.h"\nint alpha(void) { return 1; }\n')
        work.overlay(self.project, directory)
        header.write_text("typedef unsigned int Recovery;\n")
        return source

    def test_stale_overlay_names_redraft_with_original_scratch(self):
        source = self.stale_source()

        def refuse(*args, **kwargs):
            work.overlay_data(self.project, source)

        with patch("unbake.decomp.trial.retain_draft", side_effect=refuse):
            code, out, error = self.run_main(self.args("try", str(source)))
        self.assertEqual((code, error), (1, ""))
        self.assertIn("trial.overlay_stale", out)
        self.assertEqual(self.action(out)[-4:], ["draft", "alpha", "--scratch", str(self.scratch)])
        self.assertNotIn("Supply", out)

    def test_stale_draft_can_be_redrafted_and_old_source_is_archived(self):
        source = self.stale_source()
        original = source.read_bytes()
        generated = self.directory / "generated/alpha.c"
        generated.parent.mkdir()
        generated.write_text("int alpha(void) { return 2; }\n")
        work.overlay(self.project, generated.parent)
        version = self.project.versions[0]
        with (
            patch.object(draft, "owning_versions", return_value=[version]),
            patch.object(draft, "inputs", return_value=nullcontext({version: (self.project.build, generated)})),
            patch("unbake.decomp.type_context.snapshot", return_value=("", "")),
            patch("unbake.decomp.type_context.redrafts", return_value=[]),
            patch.object(m2c, "draft", return_value=generated),
            patch.object(work, "identity", return_value={"subject": "alpha"}),
        ):
            code, out, error = self.run_main(self.args("draft", "alpha", "--scratch", str(self.scratch)))
        self.assertEqual((code, error), (0, ""))
        self.assertEqual(source.read_bytes(), generated.read_bytes())
        archived = list(self.scratch.glob("alpha.redraft.*/alpha.c"))
        self.assertEqual(len(archived), 1)
        self.assertEqual(archived[0].read_bytes(), original)
        self.assertEqual(self.action(out)[-4:], ["try", str(source), "--scratch", str(self.scratch)])

    def test_existing_current_draft_names_try_and_keeps_it(self):
        source = self.stale_source()
        data = json.loads((source.parent / "overlay.json").read_bytes())
        for name in data["base"]:
            staged = source.parent / "overlay" / name
            if staged.exists():
                (self.root / name).write_bytes(staged.read_bytes())
        with (
            patch.object(draft, "owning_versions", return_value=[self.project.versions[0]]),
            patch("unbake.decomp.type_context.snapshot", return_value=("", "")),
            patch("unbake.decomp.type_context.redrafts", return_value=[]),
            patch.object(m2c, "draft") as generate,
        ):
            code, out, error = self.run_main(self.args("draft", "alpha", "--scratch", str(self.scratch)))
        self.assertEqual((code, error), (1, ""))
        generate.assert_not_called()
        self.assertEqual(self.action(out)[-4:], ["try", str(source), "--scratch", str(self.scratch)])

    def test_draft_selection_refusals_name_next_action(self):
        for operands, next_phase in (
            ((), "next"),
            (("bad-name",), "next"),
            (("alpha", "--struct", "record"), "--help"),
        ):
            with self.subTest(operands=operands):
                code, out, error = self.run_main(self.args("draft", *operands))
                self.assertEqual((code, error), (1, ""))
                self.assertEqual(self.action(out)[-1], next_phase)
        (self.root / "unbake-exclusions.json").write_text(json.dumps({"schema": 1, "functions": ["alpha"]}))
        code, out, error = self.run_main(self.args("draft", "alpha"))
        self.assertEqual((code, error), (1, ""))
        self.assertEqual(self.action(out)[-1], "next")

    def test_m2c_boundary_keeps_function_action_and_failed_candidate_path(self):
        candidate = self.directory / "compile-proof/alpha.c"
        candidate.parent.mkdir()
        candidate.write_text("void alpha(void *p) { *p = 1; }\n")
        refusal = Held(
            "m2c",
            f"m2c/type compile proof failed: invalid use of void expression\ndraft_path: {candidate}",
            next_action="unbake draft alpha --scratch " + shlex.quote(str(self.scratch)),
        )
        version = self.project.versions[0]
        with (
            patch.object(draft, "owning_versions", return_value=[version]),
            patch.object(draft, "inputs", return_value=nullcontext({version: (self.project.build, candidate)})),
            patch("unbake.decomp.type_context.snapshot", return_value=("", "")),
            patch.object(m2c, "_draft", side_effect=refusal),
        ):
            code, out, error = self.run_main(self.args("draft", "alpha", "--scratch", str(self.scratch)))
        self.assertEqual((code, error), (1, ""))
        self.assertIn("HELD(m2c): alpha: m2c/type compile proof failed", out)
        self.assertIn(f"draft_path: {candidate}", out)
        self.assertTrue(candidate.is_file())
        self.assertEqual(self.action(out)[-4:], ["draft", "alpha", "--scratch", str(self.scratch)])
        self.assertNotIn("Supply", out)

    def test_source_rule_receipt_keeps_edit_action_and_required_scratch(self):
        self.source.write_text("void alpha(void) { p->words.w0 = 0xE7000000; }\n")
        with patch("unbake.decomp.trial.trial_inputs") as compile:
            code, out, error = self.run_main(self.args("try", str(self.source)))
        self.assertEqual((code, error), (1, ""))
        compile.assert_not_called()
        self.assertIn(f"Next: Edit {self.source}. Then run ", out)
        self.assertIn("--scratch " + str(self.scratch), out.splitlines()[-1])

    def test_early_m2c_failures_keep_function_and_action_without_inventing_path(self):
        version = self.project.versions[0]
        for refusal in (
            Held("m2c", "context.types: missing"),
            Held("m2c", "alpha: invalid types"),
            OSError("cannot read context"),
        ):
            with (
                self.subTest(refusal=str(refusal)),
                patch.object(draft, "owning_versions", return_value=[version]),
                patch.object(draft, "inputs", return_value=nullcontext({version: (self.project.build, self.source)})),
                patch("unbake.decomp.type_context.snapshot", return_value=("", "")),
                patch.object(m2c, "_draft", side_effect=refusal),
            ):
                code, out, error = self.run_main(self.args("draft", "alpha", "--scratch", str(self.scratch)))
            self.assertEqual((code, error), (1, ""))
            self.assertIn("HELD(m2c): alpha:", out)
            self.assertNotIn("draft_path:", out)
            self.assertEqual(self.action(out)[-4:], ["draft", "alpha", "--scratch", str(self.scratch)])

    def test_failed_redraft_retains_old_draft_and_other_overlay_refusal_propagates(self):
        source = self.stale_source()
        original = source.read_bytes()
        version = self.project.versions[0]
        with (
            patch.object(draft, "owning_versions", return_value=[version]),
            patch.object(draft, "inputs", return_value=nullcontext({version: (self.project.build, source)})),
            patch("unbake.decomp.type_context.snapshot", return_value=("", "")),
            patch("unbake.decomp.type_context.redrafts", return_value=[]),
            patch.object(m2c, "draft", side_effect=Held("m2c", "type issue", next_action="unbake draft alpha")),
        ):
            code, out, _ = self.run_main(self.args("draft", "alpha", "--scratch", str(self.scratch)))
        self.assertEqual(code, 1)
        self.assertEqual(self.action(out)[-2:], ["draft", "alpha"])
        self.assertEqual(source.read_bytes(), original)
        self.assertFalse(list(self.scratch.glob("alpha.redraft.*")))
        with (
            patch.object(draft, "owning_versions", return_value=[version]),
            patch("unbake.decomp.type_context.snapshot", return_value=("", "")),
            patch("unbake.decomp.type_context.redrafts", return_value=[]),
            patch.object(
                work,
                "overlay_data",
                side_effect=Held("try", "trial.overlay: escaping header", next_action="unbake draft --help"),
            ),
            patch.object(m2c, "draft") as generate,
        ):
            code, out, _ = self.run_main(self.args("draft", "alpha", "--scratch", str(self.scratch)))
        self.assertEqual(code, 1)
        self.assertEqual(self.action(out)[-2:], ["draft", "--help"])
        generate.assert_not_called()

    def test_rom_refusal_keeps_explicit_project_and_policy_retry(self):
        policy = self.directory / "operator policy.toml"
        with patch.dict("os.environ"), patch("unbake.cli.setup.run", side_effect=Held("setup", "setup.roms: no files")):
            code, out, error = self.run_main(["--project", str(self.root), "--policy", str(policy), "setup"])
        self.assertEqual((code, error), (1, ""))
        self.assertIn(f"Next: Put ROMs in {self.project.roms}.", out)
        retry = out.split("Then run ", 1)[1].removesuffix(".\n")
        self.assertEqual(shlex.split(retry), ["unbake", "--project", str(self.root), "--policy", str(policy), "setup"])

    def test_explicit_redraft_archives_current_source_and_remembers_private_scratch(self):
        source = self.stale_source()
        original = source.read_bytes()
        generated = self.directory / "generated/alpha.c"
        generated.parent.mkdir()
        generated.write_text("int alpha(void) { return 3; }\n")
        work.overlay(self.project, generated.parent)
        version = self.project.versions[0]
        with (
            patch.object(draft, "owning_versions", return_value=[version]),
            patch.object(draft, "inputs", return_value=nullcontext({version: (self.project.build, generated)})),
            patch("unbake.decomp.type_context.snapshot", return_value=("", "")),
            patch.object(m2c, "draft", return_value=generated),
            patch.object(work, "identity", return_value={"subject": "alpha"}),
        ):
            code, out, error = self.run_main(self.args("draft", "alpha", "--scratch", str(self.scratch), "--redraft"))
            self.assertEqual((code, error), (0, ""))
            code, out, error = self.run_main(self.args("draft", "alpha", "--redraft"))
            self.assertEqual((code, error), (0, ""))
        self.assertEqual(source.read_bytes(), generated.read_bytes())
        archives = list(self.scratch.glob("alpha.redraft.*/alpha.c"))
        self.assertEqual(len(archives), 2)
        self.assertIn(original, [path.read_bytes() for path in archives])
        self.assertEqual(self.action(out)[-2:], ["--scratch", str(self.scratch)])

    def test_failed_generation_records_attempt_for_new_selection(self):
        from unbake.decomp import draft_presence

        with (
            patch.object(draft, "owning_versions", return_value=[self.project.versions[0]]),
            patch.object(
                draft, "inputs", return_value=nullcontext({self.project.versions[0]: (self.project.build, self.source)})
            ),
            patch("unbake.decomp.type_context.snapshot", return_value=("", "")),
            patch.object(m2c, "draft", side_effect=Held("m2c", "unsupported register")),
        ):
            code, _, error = self.run_main(self.args("draft", "alpha", "--scratch", str(self.scratch)))
        self.assertEqual((code, error), (1, ""))
        self.assertEqual(draft_presence.private(self.project, self.policy)["alpha"], "failed draft attempt")
