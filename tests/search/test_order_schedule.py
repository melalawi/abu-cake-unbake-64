import hashlib
import os
import subprocess
import sys
import tempfile
import time
import tomllib
import unittest
from collections.abc import Iterable, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from unbake.decomp import explain, score, trial, trial_compile
from unbake.decomp.explain import Allocation
from unbake.decomp.trial_compare import Compare, compare_words
from unbake.families.gcc.schedule import schedule
from unbake.families.ido.schedule import schedule as ido_schedule
from unbake.project import toolchain
from unbake.project.config import Held, Policy, Project
from unbake.search import core, order
from unbake.search.core import Context, Mutation


def proposals(source: str, **context: Any) -> list[Mutation]:
    return list(
        order.propose(source, SimpleNamespace(function="f"), SimpleNamespace(deadline=time.monotonic() + 30, **context))
    )


def project_fixture(root: Path) -> SimpleNamespace:
    with Path(os.environ["UNBAKE_POLICY"]).open("rb") as stream:
        cpp = tomllib.load(stream)["cpp"]
    (root / "include").mkdir(parents=True)
    (root / "src").mkdir()
    (root / "include/types.h").write_text("typedef int Word;\n")
    (root / "config.toml").write_text(
        '''[build]
as="as"
ld="ld"
objcopy="objcopy"
splat="splat"
asflags=[]
sn64_asflags=[]
cpp="'''
        + cpp
        + """"
cppflags=["-P", "-undef", "-nostdinc"]
[build.unit_cflags]
f=["-DUNIT_VALUE=7"]
"""
    )
    compiler = SimpleNamespace(id="gcc-2.7.2-kmc", kind="sn64", cc=Path(sys.executable), cflags=("-O2",))
    return SimpleNamespace(
        root=root,
        src=root / "src",
        include=(root / "include",),
        tools=root / "tools",
        name="fixture",
        versions=("us", "eu"),
        compilers={compiler.id: compiler},
        compiler_for=lambda source: compiler,
        version=lambda version: SimpleNamespace(macros=("VALUE=" + ("3" if version == "us" else "5"),)),
        build_link=lambda version: root / version,
    )


class SearchIntegrationTests(unittest.TestCase):
    def test_recipe_preprocessing_and_candidate_context(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = project_fixture(root / "project")
            policy = SimpleNamespace(state_root=root / "state", search_beam=2, stall_trials=2)
            source = root / "f.c"
            source.write_text(
                '#include "types.h"\n/* FAKEMATCH: measured lifetime. */\nWord f(void){return VALUE+UNIT_VALUE;}\n'
            )
            for version, value in (("us", "3"), ("eu", "5")):
                with self.subTest(version=version):
                    expanded = core.preprocess(project, policy, source, version, time.monotonic() + 30)
                    self.assertRegex(expanded, r"return\s+" + value + r"\s*\+\s*7;")
                    self.assertIn("typedef int Word;", expanded)
                    self.assertIn("FAKEMATCH:", expanded)
                    self.assertNotIn("#include", expanded)
            allocation = Allocation((), (), (), ())

            def compile_trial(project: Project, policy: Policy, path: Path, scratch: Path) -> trial.Trial:
                words = [0x03E00008, 0]
                comparisons = {
                    version: compare_words(
                        version, words, words if "confirmed" in path.read_text() else [0x03E00008, 1]
                    )
                    for version in project.versions
                }
                for version in project.versions:
                    work = scratch / version
                    work.mkdir(parents=True)
                    (work / "trial.elf").write_bytes(b"ELF")
                    for name in ("baserom", "draft"):
                        (work / (name + ".bin")).write_bytes(bytes.fromhex("03e0000800000000"))
                return trial.Trial("f", hashlib.sha256(path.read_bytes()).hexdigest(), comparisons, [], "try again", [])

            seen = []

            class Generator:
                def propose(self, expanded: str, result: trial.Trial, context: Context) -> Iterator[Mutation]:
                    seen.append(context)
                    self_test.assertIs(context.allocation, allocation)
                    self_test.assertEqual(context.source.read_text(), source.read_text())
                    self_test.assertNotIn("#include", expanded)
                    self_test.assertLess(context.deadline, end)
                    yield Mutation("fixture", "candidate", expanded + "/* confirmed */")

            self_test = self
            end = time.monotonic() + 30
            with (
                patch.object(trial, "try_draft", side_effect=compile_trial),
                patch.object(explain, "allocation", return_value=allocation) as evidence,
                patch.object(score, "fuzzy", return_value=90.0),
            ):
                result = core.run(project, policy, source, [Generator()], root / "out", 10)
            self.assertTrue(result.trial.identical_everywhere)
            self.assertEqual(result.trials, 2)
            self.assertEqual(evidence.call_args.args[2], seen[0].source)
            self.assertEqual(len((root / "out/steps.jsonl").read_text().splitlines()), 2)

    def test_explain_schedule_uses_family_and_selected_function(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = project_fixture(root / "project")
            source = root / "f.c"
            source.write_text("void f(void){}")
            policy = SimpleNamespace(state_root=root / "state")
            for family in ("gcc", "ido"):
                with self.subTest(family=family):
                    compiler = project.compiler_for(source)
                    compiler.id = "gcc-2.7.2-kmc" if family == "gcc" else "ido-7.1"

                    def diagnostics(command: list[str], work: Path, phase: str) -> str:
                        if "-o" in command:
                            for stage in ("sched2", "dbr"):
                                (work / ("source." + stage)).write_text(
                                    ";; Function other\n(insn 99 0 0 (return))\n;; Function f\n(insn 9 0 0 (return))\n"
                                )
                        return "void f(void){}"

                    with (
                        patch.object(toolchain, "verify", return_value={}),
                        patch.object(trial_compile, "run_tool", side_effect=diagnostics),
                    ):
                        result = explain.order(project, policy, source, "us")
                    self.assertEqual(result.available, family == "gcc")
                    self.assertEqual(tuple(row.uid for row in result.sched2), (9,) if family == "gcc" else ())

    def test_allocation_dumps_come_from_the_build_compiler(self) -> None:
        # The pinned native SN64 cc1 writes lreg/greg itself; no separate diagnostic compiler exists.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = project_fixture(root / "project")
            config = (root / "project" / "config.toml").read_text()
            (root / "project" / "config.toml").write_text(
                config.replace(f'cpp="{tomllib.loads(config)["build"]["cpp"]}"', 'cpp="policy:cpp"')
            )
            source = root / "f.c"
            source.write_text("int f(int a){return a;}")
            compiler = project.compiler_for(source)
            compiler.id = "gcc-2.8.1-sn64"
            policy = SimpleNamespace(state_root=root / "state", cpp=Path("/usr/bin/cpp"))
            commands: list[list[str]] = []

            def tools(command: list[str], work: Path, phase: str) -> str:
                commands.append(command)
                if "-o" in command:
                    (work / "source.i.lreg").write_text(
                        ";; Function f\nRegister 80 used 2 times across 4 insns; GR_REGS or none.\n"
                    )
                    (work / "source.i.greg").write_text(
                        ";; Function f\n;; 1 regs to allocate: 80\n;; Register dispositions:\n80 in 2\n"
                    )
                return "int f(int a){return a;}"

            proof = trial.Trial("f", "0" * 64, {"us": Compare("us", 1, 1, {}, [])}, [], "", [])
            with (
                patch.object(toolchain, "verify", return_value={}),
                patch.object(trial_compile, "run_tool", side_effect=tools),
                patch.object(trial, "try_draft", return_value=proof),
            ):
                result = explain.allocation(project, policy, source, "us")  # type: ignore[arg-type]
            self.assertEqual([p.hard for p in result.pseudos], [2])
            self.assertEqual(commands[0][0], "/usr/bin/cpp")
            self.assertEqual(commands[1][0], str(compiler.cc))


class OrderTests(unittest.TestCase):
    def equivalent(self, source: str, mutations: Iterable[Mutation]) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for mutation in mutations:
                candidate = mutation.source.replace(" f(", " candidate(", 1)
                path = root / "case.c"
                binary = root / "case"
                path.write_text(
                    source
                    + "\n"
                    + candidate
                    + """
int main(void) {
    int value, condition;
    for (value=-4; value<=4; ++value)
        for (condition=0; condition<=2; ++condition)
            if (f(value, condition) != candidate(value, condition)) return 1;
    return 0;
}
"""
                )
                result = subprocess.run(
                    ["cc", "-std=c89", "-pedantic-errors", "-O2", str(path), "-o", str(binary)],
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(result.returncode, 0, mutation.description + result.stderr)
                self.assertEqual(subprocess.run([str(binary)]).returncode, 0, mutation.description)

    def test_structural_forms_preserve_semantics(self) -> None:
        cases = [
            ("int f(int value,int condition){int a;int b;a=value+2;b=condition*3;return a+b;}", "statement order"),
            (
                "int f(int value,int condition){int x;if(condition){x=value+3;}else{x=value-4;}return x;}",
                "if/else to ternary",
            ),
            ("int f(int value,int condition){int x;x=condition ? value+3 : value-4;return x;}", "ternary to if/else"),
            (
                "int f(int value,int condition){int x;if(condition){x=value+3;}else{x=value-4;}x+=2;return x;}",
                "expand shared tail",
            ),
            (
                "int f(int value,int condition){int x;x=value;if(condition)goto tail;x*=2;tail:x+=1;return x;}",
                "expand labelled tail",
            ),
            (
                "int f(int value,int condition){int x;condition+=1;x=value+2;return x+condition;}",
                "shorten local lifetime",
            ),
        ]
        for source, expected in cases:
            with self.subTest(form=expected):
                mutations = proposals(source)
                self.assertIn(expected, [row.description for row in mutations])
                self.assertEqual(len(mutations), len({row.source for row in mutations}))
                self.equivalent(source, mutations)

    def test_dependencies_aliases_and_volatile_block_statement_motion(self) -> None:
        for source in [
            "void f(int x){int y;x=2;y=x+1;}",
            "void f(int x){int y;x=y;y=x;}",
            "int global;void f(int *p){global=1;*p=2;}",
            "void f(int x){volatile int a;int b;a=x;b=x+1;}",
            "void f(int x){int a;int b;int *p;p=&a;a=x;b=*p;}",
            "void g(void);void f(int x){x=1;g();}",
            "void f(int x){int y; y=1; {int y;y=2;x=3;}}",
            "void f(int x){int a[2];int b;a[0]=1;b=x;}",
            "typedef volatile int V;typedef V W;void f(int x){W a;int b;a=x;b=x+1;}",
            "struct S{volatile int v;};void f(int x){struct S a;int b;a.v=x;b=x+1;}",
        ]:
            with self.subTest(source=source):
                self.assertNotIn("statement order", [row.description for row in proposals(source)])

    def test_mixed_arithmetic_types_do_not_change_conditional_conversions(self) -> None:
        for source in [
            "void f(int condition){long long x;if(condition){x=9007199254740993LL;}else{x=0.0;}}",
            "void f(int condition){long long x;x=condition ? 9007199254740993LL : 0.0;}",
        ]:
            with self.subTest(source=source):
                self.assertFalse(any("ternary" in row.description for row in proposals(source)))

    def test_real_clamp_and_expected_identifier_shapes(self) -> None:
        # Small statement fixtures from func_8027B790, not a full project input.
        fixtures = [
            "void f(float temp_f0,float var_f2){if(!(temp_f0<=var_f2)){var_f2=temp_f0;}}",
            "void f(int temp_v1){int expectedId;if(temp_v1<0x2E){expectedId=0xB;}else{expectedId=0x41E;}}",
            "void f(float temp_f1){int a;a=(temp_f1<0.0f ? 0 : (int)temp_f1);}",
        ]
        for source in fixtures:
            with self.subTest(source=source):
                made = proposals(source)
                if "else" in source:
                    self.assertIn("if/else to ternary", [row.description for row in made])
                self.assertTrue(all(row.kind == "order" for row in made))

    def test_comments_literals_and_ranked_source_focus(self) -> None:
        source = (
            "int f(int value,int condition){int x;/* FAKEMATCH: lifetime reason. */"
            "if(condition){x=value+1;}else{x=value-2;}return x;}\n"
        )
        for row in proposals(source, focus_lines=(1,)):
            self.assertIn("/* FAKEMATCH: lifetime reason. */", row.source)
        source = 'void g(char*);void f(int x){g("/* literal */");}'
        self.assertEqual(proposals(source), [])
        source = "int f(int value,int condition){int a;int b;\na=value;\nb=condition;\nreturn a+b;}"
        self.assertTrue(proposals(source, focus_lines=(3,)))

    def test_missing_parser_is_named(self) -> None:
        with patch.dict(sys.modules, {"pycparser": None}), self.assertRaisesRegex(Held, "pycparser"):
            proposals("void f(void){}")

    def test_named_refusals(self) -> None:
        cases = [
            ("", SimpleNamespace(function="f"), SimpleNamespace(deadline=time.monotonic() + 30), "source"),
            ("void f(void){}", SimpleNamespace(), SimpleNamespace(deadline=time.monotonic() + 30), "trial.function"),
            ("void f(void){}", SimpleNamespace(function="f"), None, "context"),
            (
                "#define A 1\nvoid f(void){}",
                SimpleNamespace(function="f"),
                SimpleNamespace(deadline=time.monotonic() + 30),
                "source.preprocessed",
            ),
            (
                "void f(}",
                SimpleNamespace(function="f"),
                SimpleNamespace(deadline=time.monotonic() + 30),
                "source.syntax",
            ),
            (
                "void g(void){}",
                SimpleNamespace(function="f"),
                SimpleNamespace(deadline=time.monotonic() + 30),
                "trial.function f",
            ),
            (
                "void f(void){}",
                SimpleNamespace(function="f"),
                SimpleNamespace(deadline=time.monotonic() + 30, focus_lines=(-1,)),
                "context.focus_lines",
            ),
        ]
        cases.extend(
            ("void f(void){}", SimpleNamespace(function="f"), SimpleNamespace(deadline=value), "context.deadline")
            for value in (None, float("inf"), True)
        )
        for source, result, context, value in cases:
            with self.subTest(value=value), self.assertRaisesRegex(Held, value.replace(".", r"\.")):
                list(order.propose(source, result, context))


class ScheduleTests(unittest.TestCase):
    sched = "(insn 9 0 4 (set (reg:SI 2) (const_int 1)))\n(insn 4 9 0 (set (reg:SI 3) (reg:SI 2)))"
    delay = "(insn/s 20 0 0 (sequence [(jump_insn 9 0 4 (return)) (insn 4 9 0 (set (reg:SI 3) (reg:SI 2)))]))"

    def test_gcc_order_delay_slots_and_text_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            a, b = root / "sched2", root / "dbr"
            a.write_text(self.sched)
            b.write_text(self.delay)
            for first, last in [(self.sched, self.delay), (a, b)]:
                with self.subTest(path=isinstance(first, Path)):
                    result = schedule({"sched2": first, "dbr": last})
                    self.assertEqual(tuple(row.uid for row in result.sched2), (9, 4))
                    self.assertEqual(tuple(row.uid for row in result.dbr), (9, 4))
                    self.assertEqual(result.delay_slots, ((9, 4),))
                    self.assertEqual(result.dbr[0].kind, "jump_insn")
                    self.assertTrue(result.available)

    def test_rtl_quoted_parentheses_and_no_delay_slots(self) -> None:
        for text in ['(insn 4294967295 0 0 (asm_input "(quoted)"))', '(insn/u 1 0 0 (asm_input "escaped \\" )"))']:
            with self.subTest(text=text):
                result = schedule({"sched2": text, "dbr": text})
                self.assertEqual(result.delay_slots, ())
                self.assertEqual(len(result.dbr), 1)

    def test_each_named_schedule_refusal(self) -> None:
        cases = [
            (None, "dumps"),
            ({}, "dumps.sched2"),
            ({"sched2": self.sched}, "dumps.dbr"),
            ({"sched2": "", "dbr": self.delay}, "dumps.sched2"),
            ({"sched2": "(insn 1", "dbr": self.delay}, "truncated RTL"),
            ({"sched2": ")", "dbr": self.delay}, "unbalanced RTL"),
            ({"sched2": "(note 1 0 0)", "dbr": self.delay}, "instructions"),
            ({"sched2": "(insn nope)", "dbr": self.delay}, "instruction.uid"),
            ({"sched2": self.sched, "dbr": "(sequence [(insn 1 0 0)])"}, "sequence.delay_slot"),
            ({"sched2": self.sched + self.sched, "dbr": self.delay}, "duplicate instruction.uid"),
            ({"sched2": Path("absent-scheduling-input"), "dbr": self.delay}, "dumps.sched2"),
        ]
        for dumps, name in cases:
            with self.subTest(value=name), self.assertRaisesRegex(Held, name.replace(".", r"\.")):
                schedule(dumps)

    def test_ido_blind_capability_is_explicit(self) -> None:
        result = ido_schedule(None)
        self.assertFalse(result.available)
        self.assertEqual(result.family, "ido")
        self.assertEqual((result.sched2, result.dbr, result.delay_slots), ((), (), ()))
        self.assertTrue(result.reason)
        for dumps in ({}, {"sched2": self.sched}):
            with self.subTest(dumps=dumps), self.assertRaisesRegex(Held, "dumps"):
                ido_schedule(dumps)
