"""Real source strategies and common source/recipe episode executor; native replay only."""

import hashlib
import io
import json
import math
import shlex
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

from tests.kit import TempCase
from tests.unified.support import B, expanded_baseline, gcc_project, native_replay, phases, retained
from unbake.cli.main import make_parser
from unbake.compilers.recipe_options import UnitRecipe
from unbake.config import Held
from unbake.search import core, creative, frontier, pairs, permute
from unbake.work import compare, compare_dump
from unbake.work.score import measure_words
from unbake.work.source_scope import admit_source, materialize, scoped_project


class SearchTests(TempCase):
    def test_public_creative_search_replays_four_retained_combinations_through_option_layer(self):
        # AST-equivalent local/tail arms replay their retained experiment outputs.
        # This public workflow replay is explicitly NOT newly compiled exactness.
        source = retained("bt-baseline.c").decode()
        project, host, file = gcc_project(self.root, content=source)
        base = project.recipe_for(B).document()
        base["options"]["compile"] = ["-G8"]
        project = replace(project, units={"src/" + B + ".c": UnitRecipe.read(base)})
        calls = []
        arms = set()

        def output(text, recipe):
            scoped = "delta_conversion" in text
            duplicated = "goto text_tail;" not in text
            arms.add((scoped, duplicated))
            self.assertIn("-G8", recipe.phase("compile"))
            self.assertIn("-G0", recipe.phase("assemble"))
            name = {
                (False, False): "bt-g8.bin",
                (True, False): "bt-local-g8.bin",
                (False, True): "bt-tail.bin",
                (True, True): "bt-exact.bin",
            }[scoped, duplicated]
            return retained(name)

        with (
            native_replay(project, output, calls),
            patch(
                "unbake.compilers.drivers.run_preprocess",
                side_effect=lambda p, command, stage, **kw: expanded_baseline(),
            ),
            patch.object(core.explain, "allocation", side_effect=AssertionError("creative needs no allocator")),
        ):
            result = core.run(project, host, file, [creative], self.root / "creative", 30)
        self.assertEqual(arms, {(False, False), (True, False), (False, True), (True, True)})
        self.assertTrue(result.trial.exact)
        self.assertEqual(result.trial.compares["us"].strict["positional_words"], 0)
        self.assertEqual(result.trial.compares["us"].strict["target_bytes"], 28652)
        self.assertEqual(result.stop_reason, "exact")

    def test_actual_seventh_dead_conversion_and_nested_text_tail_are_early_safe_proposals(self):
        source = expanded_baseline()
        trial = SimpleNamespace(function=B)
        ctx = SimpleNamespace(deadline=math.inf)
        proposals = list(creative.propose(source, trial, ctx))
        early = proposals[:5]
        self.assertTrue(any(p.kind == "conversion-scope" and "delta_conversion" in p.source for p in early))
        self.assertTrue(any(p.kind == "tail-duplicate" and "goto text_tail;" not in p.source for p in proposals))
        # Actual safe scope never hoists an uninitialized value into the entry arm.
        scoped = next(p for p in proposals if p.kind == "conversion-scope")
        self.assertIn("delta_conversion =", scoped.source)
        self.assertNotEqual(
            hashlib.sha256(scoped.source.encode()).hexdigest(), hashlib.sha256(source.encode()).hexdigest()
        )

    def test_counterfactual_live_escaped_and_nonterminal_conversion_results_are_withheld(self):
        source = "int f(int x) { double d; int result; double out; d=x; out=d; result=(int)d; return result; }"
        ctx = SimpleNamespace(deadline=math.inf)
        trial = SimpleNamespace(function="f")
        for value in (
            source,
            source.replace("return result;", "consume(&d); return 0;"),
            source.replace("return result;", "if(x) return 0;"),
        ):
            with self.subTest(counterfactual=value):
                self.assertFalse(any(p.kind == "conversion-scope" for p in creative.propose(value, trial, ctx)))

    def test_real_structural_losing_parents_survive_pareto_retention(self):
        target = retained("bt-target.bin")
        rows = []
        for kind, name in (
            ("baseline", "bt-baseline.bin"),
            ("conversion-scope", "bt-split.bin"),
            ("tail-duplicate", "bt-tail.bin"),
            ("composition", "bt-exact.bin"),
        ):
            measurement = measure_words("us", target, retained(name))
            trial = SimpleNamespace(compares={"us": measurement}, preconditions=[])
            rows.append(SimpleNamespace(kind=kind, identity=name, trial=trial))
        self.assertEqual({p.kind for p in frontier.retain(rows, 8, rows[0])}, {p.kind for p in rows})
        self.assertEqual(frontier.coordinates(rows[0].trial)[0], 771)
        # The frozen split-only output is a losing structural parent: 6459
        # positional mismatches. The separate historical 704 count is not this blob.
        self.assertEqual(frontier.coordinates(rows[1].trial)[0], 6459)
        with self.assertRaisesRegex(ValueError, "minimum width"):
            frontier.retain(rows, 3, rows[0])

    def test_explicit_option_episode_reuses_baseline_and_keys_source_recipe_dependencies(self):
        project, host, file = gcc_project(self.root, content="int func_800B1520_us(void) { return 1; }\n")
        calls = []
        with native_replay(project, lambda text, recipe: retained("bt-baseline.bin"), calls):
            baseline = compare.measure(project, host, file)
            pairs_executor = pairs.Pairs(project, host, admit_source(project, file), lambda row: None)
            first = pairs_executor.evaluate(file.read_text(), "baseline", "retained", project, baseline=baseline)
            self.assertEqual(len(calls), 1)
            self.assertIs(first.trial, baseline)
            self.assertIn("us", first.trial.facts)
            self.assertTrue(first.trial.facts["us"]["option_evidence"])
            self.assertIs(pairs_executor.evaluate(file.read_text(), "baseline", "duplicate", project), first)
            self.assertEqual(len(calls), 1)
            recipe = UnitRecipe.read({"compiler": project.default_compiler, "options": phases(compile=["-G8"])})
            other = pairs_executor.evaluate(
                file.read_text(), "baseline", "option", replace(project, units={"src/" + B + ".c": recipe})
            )
            self.assertNotEqual(first.identity, other.identity)
            self.assertEqual(len(calls), 2)
            file.write_text(file.read_text() + "\n")
            third = pairs_executor.evaluate(file.read_text(), "baseline", "changed source", project)
            self.assertNotEqual(first.identity, third.identity)
        self.assertEqual(pairs_executor.counters["pair_cache_hits"], 1)

    def test_public_search_plateau_runs_shared_bounded_episode_without_allocation_or_dumps(self):
        project, host, file = gcc_project(self.root, content="int func_800B1520_us(void) { return 1; }\n")

        class NoMutation:
            name = "retained finite source exhaustion"

            def propose(self, source, trial, ctx):
                return iter(())

        calls = []
        # Use an explicit saved reference so mocking cannot change executor behavior.
        owner = pairs.Pairs.episode
        with (
            native_replay(project, lambda text, recipe: retained("bt-baseline.bin"), calls),
            patch.object(core.explain, "allocation", side_effect=AssertionError("unneeded allocation")),
            patch.object(compare_dump, "collect", side_effect=AssertionError("unneeded dump")),
            patch.object(pairs.Pairs, "episode", autospec=True, side_effect=owner) as observed,
        ):
            result = core.run(project, host, file, [NoMutation()], self.root / "search", 30)
        self.assertGreaterEqual(observed.call_count, 1)
        self.assertLessEqual(result.telemetry["search"]["option_pairs"], observed.call_count * 8 * 4)
        self.assertTrue(
            any(json.loads(line).get("kind") == "option.episode" for line in result.steps.read_text().splitlines())
        )
        self.assertFalse(result.trial.exact)
        self.assertTrue(result.frontier)

    def test_actual_rw_six_word_plateau_opens_options_and_keeps_unmeasured_recipes_honest(self):
        name = "func_8027DD48_de"
        # Already preprocessed retained input removes host include prerequisites;
        # the native process boundary still observes this actual function body.
        import re

        source = re.sub(r"^\s*#.*$", "", retained("rw-plateau.i").decode(), flags=re.M)
        project, host, file = gcc_project(self.root, function=name, content=source, target=retained("rw-target.bin"))
        calls = []

        class Exhausted:
            name = "retained RW structural plateau"

            def propose(self, source, trial, ctx):
                return iter(())

        def output(text, recipe):
            if (
                "-G0" not in recipe.phase("compile")
                or "-O2" not in recipe.phase("compile")
                or any(t.startswith("-f") for t in recipe.phase("compile"))
            ):
                from unbake.process import named

                raise Held(
                    named(
                        "replay.unmeasured",
                        "retained packet does not measure this recipe",
                        owner="test replay",
                        stage="options",
                    )
                )
            return retained("rw-plateau.bin")

        with native_replay(project, output, calls):
            result = core.run(project, host, file, [Exhausted()], self.root / "rw-search", 30)
        self.assertEqual(result.trial.compares["us"].strict["positional_words"], 6)
        self.assertFalse(result.trial.exact)
        self.assertGreater(result.telemetry["search"]["option_pairs"], 1)
        rows = [json.loads(line) for line in result.steps.read_text().splitlines()]
        self.assertTrue(any(row.get("kind") == "option.episode" for row in rows))
        self.assertTrue(any(cap["state"] == "tool_refused" for row in rows for cap in row.get("capabilities", [])))

    def test_external_scope_refuses_before_work_and_saved_action_preserves_recipe_versions_and_roots(self):
        project, host, file = gcc_project(self.root, content="int func_800B1520_us(void) { return 1; }\n")
        external = self.root / "external"
        external.mkdir()
        source = external / (B + ".c")
        source.write_bytes(file.read_bytes())
        with self.assertRaises(Held) as held:
            admit_source(project, source)
        self.assertEqual(held.exception.key, "source.external_root")
        view = scoped_project(project, source, (external,), (external,))
        scope = admit_source(view, source)
        saved = materialize(view, scope, source.read_text())
        saved.with_suffix(".recipe.json").write_text(json.dumps(view.recipe_for(scope.unit).document()))
        action = scope.saved_action(saved, exact=False, config=self.root / "host.toml", required_versions=("us",))
        parsed = make_parser().parse_args(shlex.split(action)[1:])
        self.assertEqual(parsed.file, saved)
        self.assertEqual(parsed.require_version, ["us"])
        self.assertIn(external, parsed.source_root)
        self.assertIn(external, parsed.include_root)
        self.assertEqual(parsed.recipe, saved.with_suffix(".recipe.json"))
        self.assertEqual(scope.unit, "src/" + B + ".c")
        from unbake.cli import compare as compare_cli
        from unbake.cli.args import Context as CliContext

        ctx = CliContext("compare", parsed, project.root, None, io.StringIO(), host)
        calls = []
        with (
            native_replay(project, lambda text, recipe: retained("bt-baseline.bin"), calls),
            patch.object(CliContext, "project", return_value=project),
            patch("unbake.steps.ensure"),
        ):
            result = compare_cli.run(ctx)
        self.assertFalse(result.data["exact"])
        self.assertEqual(len(calls), 1)

    def test_external_deadline_before_start_reports_no_execution_with_explicit_stop(self):
        with patch.object(permute.native_process, "managed_group", side_effect=AssertionError("must not start")):
            result = permute._run(["retained-permuter"], self.root, {}, 0, self.root / "permuter.log")
        self.assertFalse(result.ran)
        self.assertIsNone(result.returncode)
        self.assertEqual(result.stop_reason, "deadline_before_start")
