"""Public CLI evidence for bulk naming and the owner fuzzy threshold."""

import hashlib
import io
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from unittest.mock import patch

from tests.cli.support import MainCase
from tests.match.support import MatchFixture
from unbake.cli import main
from unbake.decomp import drafts, fuzzy_bar, trial
from unbake.decomp.trial_compare import TYPES, Compare, compare_object
from unbake.layout import name_transaction
from unbake.match import nonmatching, staging
from unbake.project import build, config
from unbake.project.config import Held


def comparison(version="us", exact=9, total=10, **differences):
    typed = dict.fromkeys(TYPES, 0)
    typed.update(differences)
    return Compare(version, exact, total, typed, [], 100.0, ())


class GateCliTests(MainCase):
    def test_try_reports_exact_boundary_and_each_forbidden_difference(self):
        for difference in (None, "immediate", "changed", "inserted", "missing"):
            with self.subTest(difference=difference):
                comparisons = {
                    v: comparison(v, **({difference: 1} if difference else {"register": 1}))
                    for v in self.project.versions
                }
                result = trial.Trial("alpha", "a" * 64, comparisons, [], "")
                with patch.object(trial, "retain_draft", return_value=result):
                    code, out, err = self.run_main(self.args("try", str(self.source)))
                self.assertEqual(code, 0, out + err)
                self.assertIn("exact words 9/10 (90.000000%)", out)
                self.assertIn("owner fuzzy bar: " + ("FAIL" if difference else "PASS"), out)
                if difference:
                    self.assertIn(f"typed.{difference}=1", out)
                else:
                    self.assertIn("Next: unbake", out)
                    self.assertIn("next", out.split("Next:")[-1])

    def test_try_uses_each_containing_version_and_does_not_trust_objdiff_score(self):
        comparisons = {v: comparison(v, exact=9) for v in self.project.versions}
        comparisons[self.project.versions[-1]] = comparison(self.project.versions[-1], exact=8)
        with patch.object(trial, "retain_draft", return_value=trial.Trial("alpha", "a" * 64, comparisons, [], "")):
            _, out, _ = self.run_main(self.args("try", str(self.source)))
        self.assertIn("owner fuzzy bar: FAIL", out)
        self.assertIn("below 90%", out)


class GateAdmissionTests(MatchFixture):
    def fuzzy(self, exact=9, **differences):
        source = self.draft("alpha", '#include "types.h"\nint alpha(void) { return 0; }\n')
        result = trial.Trial(
            "alpha",
            drafts.source_identity(source.read_bytes()),
            {v: comparison(v, exact=exact, **differences) for v in self.versions},
            [],
            "unbake try alpha.c",
        )
        self.store.add(result, source, {v: 100 for v in self.versions})
        return source

    def public_submit(self, source):
        stdout, stderr = io.StringIO(), io.StringIO()
        with (
            patch.object(config, "load", return_value=self.project),
            patch.object(config, "load_policy", return_value=self.policy),
            patch("unbake.cli.guidance.resolve", return_value="unbake next"),
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            code = main.main(["--project", str(self.root), "submit", str(source)])
        return code, stdout.getvalue() + stderr.getvalue()

    def test_public_submit_refuses_each_forbidden_difference_and_lower_version(self):
        for forbidden in ("immediate", "changed", "inserted", "missing"):
            with self.subTest(forbidden=forbidden):
                source = self.fuzzy(**{forbidden: 1})
                code, output = self.public_submit(source)
                self.assertEqual(code, 1, output)
                self.assertIn(f"typed.{forbidden}=1", output)
                self.assertFalse((self.src / "alpha.c").exists())
        source = self.fuzzy(exact=8, register=2)
        code, output = self.public_submit(source)
        self.assertEqual(code, 1)
        self.assertIn("below 90%", output)

    def test_public_submit_accepts_threshold_keeps_asm_and_guard(self):
        source = self.fuzzy(register=1)
        captured = []

        def inspect(tree, generation_for):
            captured.append((tree / "src/alpha.c").read_bytes())

        self.on_build = inspect
        splits = {v: self.project.version(v).split.read_bytes() for v in self.versions}
        code, output = self.public_submit(source)
        self.assertEqual(code, 0, output)
        self.assertIn("published as NON_MATCHING", output)
        self.assertEqual(
            (self.src / "alpha.c").read_bytes(), b"#ifdef NON_MATCHING\n" + source.read_bytes() + b"#endif\n"
        )
        self.assertEqual(splits, {v: self.project.version(v).split.read_bytes() for v in self.versions})
        self.assertEqual(self.matched(), [])
        self.assertTrue(captured)

    def test_nonmatching_requires_current_trial_source_and_headers(self):
        source = self.fuzzy(order=1)
        original = source.read_bytes()
        source.write_bytes(original + b"/* edit */\n")
        with self.assertRaisesRegex(Held, "trial.source_sha256"):
            nonmatching.admit(self.project, self.policy, source)
        source.write_bytes(original)
        header = self.project.include[0] / "types.h"
        before = header.read_bytes()
        header.write_bytes(before + b"/* edited */\n")
        with self.assertRaisesRegex(Held, "submit.overlay_sha256"):
            nonmatching.admit(self.project, self.policy, source)
        header.write_bytes(before)
        self.prove(source, identical=False)
        code, out = self.public_submit(source)
        self.assertEqual(code, 1)
        self.assertIn("below 90%", out)


class GateTransactionTests(MatchFixture):
    def setUp(self):
        super().setUp()
        versions = {
            v: replace(
                self.project.version(v),
                baserom_sha1=hashlib.sha1(self.project.version(v).baserom.read_bytes()).hexdigest(),
            )
            for v in self.versions
        }
        self.project = replace(self.project, version_map=versions)
        self.change = name_transaction.Change(self.root / "include/name.h", None, b"extern int carried;\n")
        patch.object(name_transaction.makefile, "render", return_value={}).start()
        patch.object(name_transaction, "refresh_checksums").start()
        patch.object(config, "load", side_effect=lambda tree: staging.project_at(self.project, tree)).start()

    def compiler(self, project, policy, versions, *, tree, generation_for):
        results = {}
        for version in versions:
            generation = generation_for(version)
            rom = generation / f"{project.name}.{version}.z64"
            rom.write_bytes(project.version(version).baserom.read_bytes())
            log = generation / "build.log"
            log.write_text("proof")
            results[version] = build.BuildResult(version, True, "ROM: OK", log, generation)
        return results

    def test_all_roms_prove_before_publication(self):
        with patch.object(build, "build", side_effect=self.compiler):
            result = name_transaction.apply(self.project, self.policy, [self.change])
        self.assertEqual(len(result), len(self.versions))
        self.assertEqual(self.change.path.read_bytes(), self.change.after)
        for version in self.versions:
            self.assertNotEqual(build.current_generation(self.project, version), self.original[version])

    def test_proof_generated_assembly_paths_publish_with_the_generation(self):
        generated = self.project.asm / "us/data/rodata/carried/constant.s"

        def extract(*args, **kwargs):
            tree = kwargs["tree"]
            path = tree / generated.relative_to(self.root)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b".word 0\n")
            return self.compiler(*args, **kwargs)

        with patch.object(build, "build", side_effect=extract):
            name_transaction.apply(self.project, self.policy, [self.change])
        self.assertEqual(generated.read_bytes(), b".word 0\n")

    def test_false_rom_proof_publishes_no_file_or_generation(self):
        def wrong(*args, **kwargs):
            results = self.compiler(*args, **kwargs)
            (results["eu"].generation / "fixture.eu.z64").write_bytes(b"wrong")
            return results

        with patch.object(build, "build", side_effect=wrong), self.assertRaisesRegex(Held, "split.rename.sha1.eu"):
            name_transaction.apply(self.project, self.policy, [self.change])
        self.assertFalse(self.change.path.exists())
        self.assert_untouched()

    def test_intervening_input_or_trial_change_refuses_before_writes(self):
        def mutate(*args, **kwargs):
            results = self.compiler(*args, **kwargs)
            (self.root / "include/other.h").write_bytes(b"changed")
            return results

        with patch.object(build, "build", side_effect=mutate), self.assertRaisesRegex(Held, "split.rename.stale"):
            name_transaction.apply(self.project, self.policy, [self.change])
        self.assertFalse(self.change.path.exists())
        self.assert_untouched()

    def test_publication_error_rolls_back_prior_writes(self):
        second = name_transaction.Change(self.root / "include/second.h", None, b"second")
        actual = name_transaction.atomic

        def fail(path, content):
            if path == second.path:
                raise OSError("publication write failed")
            return actual(path, content)

        with (
            patch.object(build, "build", side_effect=self.compiler),
            patch.object(name_transaction, "atomic", side_effect=fail),
            self.assertRaisesRegex(OSError, "publication write failed"),
        ):
            name_transaction.apply(self.project, self.policy, [self.change, second])
        self.assertFalse(self.change.path.exists())
        self.assertFalse(second.path.exists())
        self.assert_untouched()


class PredicateTests(MainCase):
    def test_allowed_register_order_relocation_and_integer_threshold(self):
        for kind in ("register", "order", "relocation"):
            self.assertTrue(fuzzy_bar.evaluate({"us": comparison(**{kind: 1})}, ["us"]).passed)
        self.assertFalse(fuzzy_bar.evaluate({"us": comparison(exact=89, total=100, register=11)}, ["us"]).passed)
        self.assertTrue(fuzzy_bar.evaluate({"us": comparison(exact=90, total=100, register=10)}, ["us"]).passed)
        self.assertFalse(fuzzy_bar.evaluate({"us": comparison()}, ["us", "eu"]).passed)
        self.assertFalse(fuzzy_bar.evaluate({"us": comparison()}, ["us"], ["raw-gfx:1"]).passed)


class RelocationClassificationTests(MainCase):
    def test_changed_branch_opcode_cannot_hide_as_allowed_relocation(self):
        def side(mnemonic, destination):
            return {
                "symbols": [
                    {
                        "name": "alpha",
                        "kind": "SYMBOL_FUNCTION",
                        "size": 40,
                        "match_percent": 99.0,
                        "instructions": [
                            {
                                "instruction": {
                                    "address": 0,
                                    "branch_dest": destination,
                                    "parts": [{"opcode": {"mnemonic": mnemonic}}, {"arg": {"opaque": "v0"}}],
                                }
                            }
                        ]
                        + [
                            {"instruction": {"address": i * 4, "parts": [{"opcode": {"mnemonic": "nop"}}]}}
                            for i in range(1, 10)
                        ],
                    }
                ]
            }

        result = compare_object("us", {"left": side("bnez", 16), "right": side("beqzl", 20)}, "alpha")
        self.assertEqual(result.identical, 9)
        self.assertEqual(result.typed["relocation"], 1)
        self.assertEqual(result.typed["changed"], 1)
        self.assertFalse(fuzzy_bar.evaluate({"us": result}, ["us"]).passed)


class GateBatchTests(MatchFixture):
    def test_cli_batch_proves_two_sources_and_refreshes_types_once(self):
        from unbake.cli.main import main
        from unbake.decomp import type_context

        alpha, beta = self.draft("alpha"), self.draft("beta")
        self.prove(alpha)
        self.prove(beta)
        with (
            patch("unbake.cli.main.config.load", return_value=self.project),
            patch("unbake.cli.main.config.load_policy", return_value=self.policy),
            patch.object(type_context, "feedback_many") as feedback,
        ):
            code = main(["--project", str(self.root), "submit", "--batch", str(alpha), str(beta)])
        self.assertEqual(code, 0)
        self.assertEqual(self.calls, [("alpha", "beta")])
        feedback.assert_called_once()
        self.assertEqual({entry[0] for entry in feedback.call_args.args[1]}, {"alpha", "beta"})

    def test_batch_proves_changed_source_with_other_source(self):
        from unbake.match import batch

        alpha, beta = self.draft("alpha"), self.draft("beta")
        beta.write_text("int beta(void) { return 2; }\n")
        lines = batch.publish(self.project, self.policy, [alpha, beta])
        self.assertTrue(any("beta matched on VERSION" in line for line in lines))
        self.assertTrue(any("alpha matched on VERSION" in line for line in lines))
        self.assertTrue((self.src / "alpha.c").exists())
        self.assertTrue((self.src / "beta.c").exists())
        self.assertEqual(self.calls, [("alpha", "beta")])

    def test_batch_isolates_shared_header_conflict_before_build(self):
        from unbake.match import batch

        sources = [self.draft(name) for name in ("alpha", "beta", "gamma")]
        from unbake.match import declarations

        actual = declarations.fold_source

        def conflict(project, policy, headers, function, *args, **kwargs):
            if function == "beta":
                raise Held("structs", "struct L: duplicate definition")
            return actual(project, policy, headers, function, *args, **kwargs)

        with patch.object(declarations, "fold_source", side_effect=conflict):
            lines = batch.publish(self.project, self.policy, sources)
        self.assertTrue(any("HELD(submit): beta:" in line and "struct L" in line for line in lines))
        self.assertTrue(any("alpha matched on VERSION" in line for line in lines))
        self.assertTrue(any("gamma matched on VERSION" in line for line in lines))
        self.assertFalse((self.src / "beta.c").exists())
