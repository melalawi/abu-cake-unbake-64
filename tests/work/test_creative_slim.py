"""Captured chosen compare words and terminal sources through public handlers.

Only native compilation/linking, preprocessing, allocator collection and readiness
are replaced. Scoring, recipe selection, attempts, AST proposals and search run.
"""

import argparse
import hashlib
import importlib.util
import io
import json
import re
import struct
import subprocess
import unittest
import zlib
from contextlib import ExitStack, contextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tests.kit import TempCase
from tests.project_fixture import make
from unbake import cdecl, runner
from unbake.cli import compare as compare_cli
from unbake.cli import search_variants
from unbake.cli.args import Context
from unbake.compilers.families.types import Allocation
from unbake.search import METHODS, core, order
from unbake.work import attempts, compare

FIXTURES = Path(__file__).parents[1] / "fixtures" / "creative_slim"
B = "func_800B1520_us"
C0 = "func_802800C0_de"
AC = "func_8010AC90_us"


def payloads(function, label="baseline"):
    return json.loads(zlib.decompress((FIXTURES / f"{function}--{label}.json.z").read_bytes()))


def packed(words):
    return struct.pack(f">{len(words)}I", *words)


def source(function, label="baseline"):
    return (FIXTURES / f"{function}--{label}.c").read_text()


def expanded(text):
    headers = (FIXTURES / "types.h").read_text() + (FIXTURES / "vec3.h").read_text()
    return re.sub(r"^\s*#.*$", "", headers + text, flags=re.M)


class CapturedHandlers(TempCase):
    def project_for(self, function, captured, text):
        home = self.root / f"case-{getattr(self, 'fixture_count', 0)}"
        home.mkdir()
        self.fixture_count = getattr(self, "fixture_count", 0) + 1
        project, host = make(home, versions=tuple(captured))
        for version, data in captured.items():
            config = project.version(version)
            pc = int(data["pc"], 16)
            size = len(data["target"]) * 4
            config.split.write_text(
                "name: fixture\nsegments:\n  - name: main\n    type: code\n    start: 0x40\n"
                f"    vram: 0x{pc:X}\n    subsegments:\n      - [0x40, asm, {function}]\n"
                f"  - [0x{size + 0x40:X}]\n"
            )
            names = {function: pc}
            for address, aliases in data["names"].items():
                names.update(dict.fromkeys(aliases, int(address, 16)))
            config.symbols.write_text("".join(f"{name} = 0x{address:X};\n" for name, address in names.items()))
            config.baserom.write_bytes(bytes(0x40) + packed(data["target"]))
        compiler = next(iter(captured.values()))["compiler"]
        configured = project.compilers["ido-7.1"]
        project = replace(project, default_compiler=compiler, compilers={compiler: configured})
        file = home / f"{function}.c"
        file.write_text(text)
        return project, host, file

    @contextmanager
    def native(self, provider, *, alternate=False):
        self.native_calls = []

        @contextmanager
        def compile_unit(project, host, file, version, *, unit, **options):
            self.native_calls.append((project.compiler_reference(unit), version, file.read_text()))
            yield self.root / "captured.o"

        def link(project, host, obj, version, row, file):
            data = provider(file.read_text(), version)
            candidate = (
                [0] * len(data["target"])
                if alternate and project.compiler_reference(file.stem) == "ido-7.1"
                else data["candidate"]
            )
            return packed(candidate), ["retained placement diagnostic"] * data["score"]["typed"]["relocation"]

        with patch.object(runner, "compile_unit", compile_unit), patch.object(runner, "link_function", link):
            yield

    def context(self, command, project, host, file, **args):
        return Context(command, argparse.Namespace(file=file, **args), project.root, None, io.StringIO(), host)

    def public_compare(self, function, label="baseline", *, alternate=False):
        captured = payloads(function, label)
        project, host, file = self.project_for(function, captured, source(function, label))
        if alternate:
            project = replace(
                project, compilers=project.compilers | {"ido-7.1": next(iter(project.compilers.values()))}
            )
        context = self.context("compare", project, host, file, require_version=None)
        rendered = []
        original_lines = compare.Compared.lines

        def render(measured):
            rendered.append(measured)
            return original_lines(measured)

        with ExitStack() as stack:
            stack.enter_context(self.native(lambda text, version: captured[version], alternate=alternate))
            stack.enter_context(patch.object(Context, "ready", return_value=(project, host)))
            stack.enter_context(patch.object(compare.Compared, "lines", render))
            diagnostics = raw = None
            if importlib.util.find_spec("unbake.work.compare_facts") is not None:
                from unbake.work import compare_facts

                diagnostics = stack.enter_context(
                    patch.object(compare_facts, "align_words", wraps=compare_facts.align_words)
                )
                raw = stack.enter_context(
                    patch.object(compare_facts, "SequenceMatcher", wraps=compare_facts.SequenceMatcher)
                )
            result = compare_cli.run(context)
            calls = len(self.native_calls)
            if raw is not None:
                self.assertEqual(raw.call_count, len(captured))
                self.assertEqual(
                    diagnostics.call_count,
                    sum(f["work_counts"]["diagnostic_correspondence"] for f in result.data["facts"].values()),
                )
                self.assertLessEqual(diagnostics.call_count, len(captured))
            # Repeated live-object rendering performs no measurement or alignment.
            with (
                patch.object(runner, "compile_unit", side_effect=AssertionError("renderer native work")),
                patch.object(subprocess, "run", side_effect=AssertionError("renderer subprocess")),
            ):
                for _ in range(2):
                    rendered[0].document()
                    original_lines(rendered[0])
                    json.dumps(result.document())
            self.assertEqual(len(self.native_calls), calls)
            if raw is not None:
                self.assertEqual(raw.call_count, len(captured))
                self.assertEqual(
                    diagnostics.call_count,
                    sum(f["work_counts"]["diagnostic_correspondence"] for f in result.data["facts"].values()),
                )
        for version, data in captured.items():
            self.assertEqual(result.data["versions"][version], data["score"])
        recorded = attempts.read(project, function)
        self.assertEqual(len(recorded), 1)
        self.assertEqual(recorded[0].versions, result.data["versions"])
        self.assertEqual(recorded[0].compiler, result.data["compiler"])
        return result, captured

    def conservation(self, data, total):
        self.assertEqual(data["target_different"], total)
        self.assertEqual(sum(data["buckets"].values()), total)
        self.assertEqual(sum(r["different"] for r in data["regions"]), total)
        self.assertEqual(len(data["all_word_rows"]), total)
        self.assertEqual(len({r["target_offset"] for r in data["all_word_rows"]}), total)
        self.assertEqual(sum(r["inserted_alignment_rows"] for r in data["regions"]), data["raw_inserted_rows"])
        self.assertLessEqual(sum(len(r["rows"]) + len(r["equal_context"]) for r in data["regions"]), 64)
        self.assertEqual(data["raw_target_delta_bytes"], -4 * total)
        self.assertEqual(sum(r["raw_target_delta_bytes"] for r in data["regions"]), -4 * total)
        self.assertEqual(data["work_counts"]["raw_correspondence"], 1)
        self.assertLessEqual(data["work_counts"]["diagnostic_correspondence"], 1)
        self.assertEqual(data["work_counts"]["native"], 0)
        self.assertEqual(data["work_counts"]["per_region_alignments"], 0)

    def test_b1520_chosen_recipe_shared_edge_names_totals_and_work(self):
        # This last recipe is worse: facts must bind the returned winner, not last native probe.
        result, _ = self.public_compare(B, alternate=True)
        data = result.data["facts"]["us"]
        self.conservation(data, 45)
        self.assertEqual(data["buckets"], {"structural_candidate": 15, "consequential_proven": 5, "unresolved": 25})
        self.assertEqual(len(self.native_calls), 2)
        self.assertEqual(self.native_calls[-1][0], "ido-7.1")
        self.assertEqual(data["compiler"], result.data["compiler"])
        self.assertEqual(data["source_sha256"], result.data["sha256"])
        calls = [c for r in data["regions"] for c in r["calls"]]
        self.assertTrue(
            any(c["side"] == "target" and c["offset"] == 0x1428 and "__cmpdi2" in c.get("aliases", []) for c in calls)
        )
        self.assertTrue(any(c["side"] == "target" and c["offset"] == 0x1430 for c in calls))
        self.assertIn({"offset": 0x7E8, "destination": 0x5CA8, "delay_slot": 0x7EC}, data["target_internal_edges"])
        self.assertFalse(result.data["exact"])

    def test_c0_five_versions_conserve_142_and_placement_diagnostics(self):
        result, captured = self.public_compare(C0)
        self.assertEqual(len(result.data["facts"]), 5)
        self.assertEqual(len(self.native_calls), 5)
        for version, data in result.data["facts"].items():
            self.conservation(data, 142)
            self.assertEqual(data["scored_inserted"], 3)
            self.assertEqual(result.data["versions"][version]["typed"]["relocation"], 7)
            self.assertEqual(data["pc"], captured[version]["pc"])
        self.assertEqual(
            result.data["facts"]["de"]["buckets"],
            {"structural_candidate": 27, "consequential_proven": 36, "unresolved": 79},
        )

    def test_signedness_pair_with_equal_registers_and_neighborhood(self):
        result, _ = self.public_compare("func_80200840_de", "control-before-bound")
        for data in result.data["facts"].values():
            self.conservation(data, 1)
            row = data["all_word_rows"][0]
            self.assertEqual((row["target_offset"], row["target_op"], row["candidate_op"]), (0x24, "srlv", "srav"))
            self.assertEqual(row["target_registers"], row["candidate_registers"])
            self.assertTrue(any(r["equal_context"] for r in data["regions"]))

    def test_predicate_pair_carries_surviving_equal_slti(self):
        result, _ = self.public_compare("func_8044DCC0_de", "control-before-bound")
        for data in result.data["facts"].values():
            self.conservation(data, 1)
            row = data["all_word_rows"][0]
            self.assertEqual((row["target_offset"], row["target_op"], row["candidate_op"]), (0x8C, "nop", "addu"))
            neighborhood = [c for r in data["regions"] for c in r["equal_context"]]
            self.assertTrue(any(c["target_offset"] == 0x7C and c["target_op"] == "slti" for c in neighborhood))

    def search_handler(self, function, captured, text, generator, provider):
        project, host, file = self.project_for(function, captured, text)
        context = self.context("search-variants", project, host, file, method="order", seconds=60)
        native_preprocess = []

        def preprocess(command, **options):
            path = next(Path(word) for word in command if str(word).endswith(".c"))
            native_preprocess.append(path)
            return subprocess.CompletedProcess(command, 0, expanded(path.read_text()), "")

        with (
            self.native(provider),
            patch.object(Context, "ready", return_value=(project, host)),
            patch.dict(METHODS, {"order": generator}),
            patch.object(core.subprocess, "run", preprocess),
            patch.object(core.explain, "allocation", return_value=Allocation((), (), (), ())) as allocation,
            patch.object(cdecl, "parse", wraps=cdecl.parse) as parse,
        ):
            result = search_variants.run(context)
        self.search_work = (len(native_preprocess), allocation.call_count, parse.call_count)
        return result, Path(result.data["best_file"]).read_text()

    def test_ac90_public_search_copies_all_six_edges_once_and_retains_fallthrough(self):
        before, after = payloads(AC), payloads(AC, "tail-duplicate-1")
        descriptions = []

        def propose(text, trial, context):
            for mutation in order.propose(text, trial, context):
                if mutation.description.startswith("expand labelled tail equal_normal_response"):
                    descriptions.append(mutation.description)
                    yield mutation

        def provider(text, version):
            return (before if "goto equal_normal_response" in text else after)[version]

        result, best = self.search_handler(AC, before, source(AC), SimpleNamespace(propose=propose), provider)
        self.assertEqual(len(descriptions), 2)  # incumbent revisit is an existing cached beam proposal
        self.assertEqual(len(set(descriptions)), 1)
        self.assertIn("6 goto edges; fallthrough retained", descriptions[0])
        self.assertNotIn("goto equal_normal_response", best)
        self.assertNotIn("equal_normal_response:", best)
        self.assertIn("diagonal_response:", best)
        self.assertEqual(best.count("normal->y = normal_x;"), 7)
        self.assertEqual(result.data["mutations"], 1)
        self.assertEqual(len(self.native_calls), 3)  # baseline, version probe, complete confirmation
        self.assertEqual(self.search_work, (2, 2, 4))
        self.assertFalse(result.data["exact"])
        self.assertEqual(4 * (260 - 221), 156)
        # The actual winning source has precisely the same function body.
        from pycparser import c_ast, c_generator

        printer = c_generator.CGenerator()

        def body(text):
            tree = cdecl.parse(cdecl.declaration_source(expanded(text)))
            return printer.visit(
                next(node.body for node in tree.ext if isinstance(node, c_ast.FuncDef) and node.decl.name == AC)
            )

        self.assertEqual(body(best), body(source(AC, "tail-duplicate-1")))

    def test_real_unsupported_cross_compound_and_switch_break_have_no_tail_proposal(self):
        for function in ("func_802251DC_de", "func_80430118_de"):
            with self.subTest(function=function):
                tails = []

                def propose(text, trial, context, tails=tails):
                    for mutation in order.propose(text, trial, context):
                        if mutation.description.startswith("expand labelled tail"):
                            tails.append(mutation)
                            yield mutation

                captured = payloads("func_80200840_de", "control-before-bound")
                captured = {"de": captured["de"]}
                result, best = self.search_handler(
                    function,
                    captured,
                    source(function),
                    SimpleNamespace(propose=propose),
                    lambda text, version, captured=captured: captured[version],
                )
                self.assertEqual(tails, [])
                self.assertEqual(result.data["mutations"], 0)
                self.assertEqual(best, source(function))
                self.assertEqual(len(self.native_calls), 1)
                self.assertEqual(self.search_work, (1, 1, 1))

    def test_b1520_local_28_byte_gain_cannot_accept_global_1024_byte_loss(self):
        before, after = payloads(B), payloads(B, "web-split-dead-cast")
        baseline = before["us"]

        def propose(text, trial, context):
            yield core.Mutation("captured", "retained measured trial", source(B, "web-split-dead-cast"))

        def provider(text, version):
            return (before if hashlib.sha256(text.encode()).hexdigest() == baseline["source_sha256"] else after)[
                version
            ]

        result, best = self.search_handler(B, before, source(B), SimpleNamespace(propose=propose), provider)
        self.assertEqual(best, source(B))
        self.assertEqual(result.data["mutations"], 1)
        self.assertEqual(len(self.native_calls), 2)
        self.assertFalse(result.data["exact"])
        # Both local/full diagnostic documents come through the chosen public compare boundary.
        baseline_result, _ = self.public_compare(B)
        changed_result, _ = self.public_compare(B, "web-split-dead-cast")
        bound = [baseline_result.data["facts"]["us"], changed_result.data["facts"]["us"]]
        local = [sum(0x13F0 <= row["target_offset"] < 0x1480 for row in data["all_word_rows"]) for data in bound]
        self.assertEqual(local, [8, 1])
        self.assertEqual(4 * (local[0] - local[1]), 28)
        self.assertEqual(bound[1]["raw_target_delta_bytes"] - bound[0]["raw_target_delta_bytes"], -1024)

    def test_parameters_are_in_tail_binding_checks_and_partial_labels_remain(self):
        text = "int f(int x) { if (x) { int x = 2; goto L; } L: x++; return x; }"
        found = list(order.propose(text, SimpleNamespace(function="f"), SimpleNamespace(deadline=1e15)))
        self.assertFalse(any(m.description.startswith("expand labelled tail") for m in found))
        text = "int f(int x) { if (x) goto A; A: x++; goto B; B: return x; }"
        found = list(order.propose(text, SimpleNamespace(function="f"), SimpleNamespace(deadline=1e15)))
        self.assertFalse(any(m.description.startswith("expand labelled tail A") for m in found))


if __name__ == "__main__":
    unittest.main()
