"""Shared evidence, staged resolution and measured search contracts."""

import hashlib
import json
import tempfile
import time
import unittest
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch

from tests.support import tool
from unbake.cli.decomp import run
from unbake.cli.main import make_parser
from unbake.decomp import explain, features, needs, score, trial
from unbake.decomp.drafts import Store
from unbake.decomp.trial_compare import TYPES, Compare
from unbake.families import Family, family_for
from unbake.project.config import Compiler, Held, Policy, Project, Version
from unbake.search import core, methods, permute, register


class CoreTests(unittest.TestCase):
    def test_permuter_without_improvements_retains_the_object_baseline(self) -> None:
        source = self.root / "f.c"
        source.write_text("int f(void) { return 1; }")
        project = SimpleNamespace(root=self.root / "project", versions=("us",), build_link=lambda v: self.root / v)
        policy = SimpleNamespace(search_beam=1, stall_trials=1)
        comparison = Compare("us", 1, 2, {kind: int(kind == "changed") for kind in TYPES}, [], 50.0, ())
        baseline = trial.Trial(
            "f", hashlib.sha256(source.read_bytes()).hexdigest(), {"us": comparison}, [], "try again"
        )
        generator = permute.Permuter("us", self.root / "target.o", 5)

        def completed(*args: object) -> Iterator[core.Mutation]:
            object.__setattr__(generator, "ran", True)
            return iter(())

        with (
            patch.object(core, "preprocess", return_value=source.read_text()),
            patch.object(explain, "allocation", return_value=SimpleNamespace(differences=[], pseudos=[])),
            patch.object(core, "_retain", return_value=50.0),
            patch.object(trial, "try_draft", return_value=baseline),
            patch.object(permute.Permuter, "propose", side_effect=completed),
        ):
            result = core.run(cast(Project, project), cast(Policy, policy), source, [generator], self.root / "out", 5)
        self.assertEqual(result.trials, 1)
        self.assertEqual(result.fuzzy, 50.0)
        self.assertIs(result.trial, baseline)

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.enterContext(patch.object(features, "load"))
        self.enterContext(patch.dict(needs.RESOLVERS, {}, clear=True))

    def test_evidence_roundtrip_and_named_refusals(self) -> None:
        cases: list[needs.Need] = [
            needs.SymbolNeed("us", "D_800C7C94", 0x800C7C94, -32768, ".data", "f32", 4, "3c01800d"),
            needs.LabelNeed("us", "inner", 0x80001004, "data", "3f800000"),
            needs.LayoutNeed("us", "Player", [["team", 3, "u8", 1]], "function.c", "offset"),
            needs.PlacementNeed("us", "callee", 0x1000, 0x1010, "cut", "asm", "03e00008"),
            needs.GuardFinding("inline-asm", 2, "asm", None),
        ]
        for need in cases:
            with self.subTest(kind=type(need).__name__):
                self.assertEqual(needs.decode(json.loads(json.dumps(needs.encode(need)))), need)
                with self.assertRaisesRegex(Held, needs.name(need)):
                    needs.resolve(
                        [need], cast(Project, None), cast(Policy, None), lambda *args: self.fail("write before refusal")
                    )
                row = needs.encode(need)
                field = next(key for key in row if key != "need_type")
                del row[field]
                with self.assertRaisesRegex(Held, field):
                    needs.decode(row)

    def test_family_registry_and_protocol(self) -> None:
        for ident, section, move in [
            ("gcc-2.8.1-sn64", ".rdata", "addu"),
            ("gcc-2.7.2-kmc", ".rdata", "addu"),
            ("ido-7.1", ".rodata", "or"),
        ]:
            with self.subTest(compiler=ident):
                family = family_for(ident)
                self.assertIsInstance(family, Family)
                self.assertEqual((family.rodata_section(), family.move_idiom()), (section, move))
                self.assertIn("-G0", family.probe_cflags())
        for value, refusal in [(None, "compiler.id"), ("absent", "absent")]:
            with self.subTest(value=value), self.assertRaisesRegex(Held, refusal):
                family_for(cast(str, value))

    def test_search_cli_real_loop_cache_store_and_cross_version_score(self) -> None:
        project_root = self.root / "project"
        project_root.mkdir()
        include = project_root / "include"
        include.mkdir()
        compiler = Compiler(
            "ido-7.1", "ido", Path(tool("cpp")), Path(tool("mips-linux-gnu-as")), (), project_root / "compiler.sha256"
        )
        (project_root / "config.toml").write_text(
            '[build]\nld = "ld"\nobjcopy = "objcopy"\nsplat = "splat"\nas = "as"\nasflags = []\n'
        )
        configured = {
            v: Version(v, project_root / v, "a" * 40, project_root / (v + ".yaml"), project_root / (v + ".txt"), ())
            for v in ("us", "eu")
        }
        project: Any = SimpleNamespace(
            root=project_root,
            name="fixture",
            versions=("us", "eu"),
            include=(include,),
            src=project_root / "src",
            compilers={compiler.id: compiler},
            compiler_for=lambda source: compiler,
            version=lambda v: configured[v],
            build_link=lambda v: project_root / v,
        )
        policy: Any = SimpleNamespace(search_beam=2, stall_trials=2, state_root=self.root / "state")
        source = self.root / "func_8041F2A0.c"
        source.write_text("start")
        calls = []
        words = bytes.fromhex("03e00008000000003c01800d")

        def compile_trial(
            project: Project, policy: Policy, path: Path, scratch: Path, versions: list[str] | None = None
        ) -> trial.Trial:
            content = path.read_text()
            calls.append(content)
            if content == "invalid":
                raise Held("try", "compile source invalid")
            counts = {"start": (293, 293), "lopsided": (294, 292), "better": (294, 294)}[content]
            comparisons = {}
            for version, count in zip(project.versions, counts, strict=False):
                if versions is not None and version not in versions:
                    continue
                directory = scratch / version
                directory.mkdir(parents=True)
                (directory / "trial.elf").write_bytes(b"ELF")
                for name in ("baserom", "draft"):
                    (directory / (name + ".bin")).write_bytes(words)
                typed = dict.fromkeys(TYPES, 0)
                typed["changed"] = 294 - count
                comparisons[version] = Compare(version, count, 294, typed, [], 100 * count / 294, ())
            return trial.Trial(path.stem, hashlib.sha256(path.read_bytes()).hexdigest(), comparisons, [], "try again")

        class Proposals:
            name = "fixture"

            def propose(self, source: str, result: trial.Trial, ctx: core.Context) -> Iterator[core.Mutation]:
                for text in ("start", "invalid", "lopsided", "better"):
                    yield core.Mutation("replace", text, text)

        import unbake.search as search

        with patch.dict(search.METHODS, {}, clear=True):
            register("fixture", Proposals())
            args = make_parser().parse_args(
                [
                    "decomp",
                    "search",
                    str(source),
                    "--method",
                    "fixture",
                    "--out",
                    str(self.root / "out"),
                    "--budget-seconds",
                    "5",
                ]
            )
            with (
                patch.object(trial, "try_draft", compile_trial),
                patch.object(score, "fuzzy", return_value=98.0),
                patch.object(explain, "allocation", return_value=SimpleNamespace(differences=[], pseudos=[])),
            ):
                run(args, project, policy)
            rows = [json.loads(line) for line in (self.root / "out" / "steps.jsonl").read_text().splitlines()]
            self.assertEqual([row["score"] for row in rows], [293, 293, None, 292, 294])
            self.assertTrue(rows[1]["cached"])
            self.assertEqual(calls.count("start"), 1)
            stored = Store(policy, project).rows(source.stem)
            self.assertEqual(len(stored), 2)
            self.assertTrue(stored[-1]["identical_everywhere"])
            with self.assertRaisesRegex(Held, "unknown"):
                methods("unknown")
        for attribute in ("search_beam", "stall_trials"):
            with self.subTest(missing=attribute):
                incomplete = SimpleNamespace(**{key: value for key, value in vars(policy).items() if key != attribute})
                with self.assertRaisesRegex(Held, attribute):
                    core.run(project, cast(Policy, incomplete), source, [Proposals()], self.root / "refused", 5)

    def test_mutation_budget_excludes_setup_and_confirms_only_improvements(self) -> None:
        for method in ("order", "permute", "empty"):
            with self.subTest(method=method):
                out = self.root / method
                source = self.root / "f.c"
                source.write_text("start")
                project: Any = SimpleNamespace(
                    root=self.root / "project",
                    versions=("us", "eu", "de", "eu-x", "us-rev1"),
                    build_link=lambda version: self.root / version,
                )
                policy: Any = SimpleNamespace(search_beam=2, stall_trials=2)
                clock = [0.0]
                calls: list[tuple[str, list[str] | None]] = []

                def compile_trial(
                    project: Project,
                    policy: Policy,
                    path: Path,
                    scratch: Path,
                    versions: list[str] | None = None,
                    calls: list[tuple[str, list[str] | None]] = calls,
                    clock: list[float] = clock,
                ) -> trial.Trial:
                    content = path.read_text()
                    calls.append((content, versions))
                    clock[0] += 60 if versions is None else 2
                    count = {"start": 10, "loss": 9, "lopsided": 12, "better": 11}[content]
                    compares = {
                        version: Compare(
                            version,
                            8 if content == "lopsided" and version == "eu" else count,
                            20,
                            dict.fromkeys(TYPES, 0),
                            [],
                            50.0,
                            (),
                        )
                        for version in (project.versions if versions is None else versions)
                    }
                    return trial.Trial("f", hashlib.sha256(path.read_bytes()).hexdigest(), compares, [], "try again")

                def proposals(
                    source: str, result: trial.Trial, ctx: core.Context, method: str = method
                ) -> Iterator[core.Mutation]:
                    for content in () if method == "empty" else ("loss", "lopsided", "better"):
                        yield core.Mutation("replace", content, content)

                generator: Any = SimpleNamespace(propose=proposals)
                if method == "permute":
                    generator = permute.Permuter("de", self.root / "target.o", 5)
                with (
                    patch.object(time, "monotonic", side_effect=lambda clock=clock: clock[0]),
                    patch.object(core, "preprocess", side_effect=lambda *args: args[2].read_text()),
                    patch.object(explain, "allocation", return_value=SimpleNamespace(differences=[], pseudos=[])),
                    patch.object(core, "_retain", return_value=90.0),
                    patch.object(trial, "try_draft", side_effect=compile_trial),
                    patch.object(permute.Permuter, "propose", side_effect=proposals),
                ):
                    if method == "empty":
                        with self.assertRaisesRegex(Held, "zero mutations evaluated"):
                            core.run(cast(Project, project), cast(Policy, policy), source, [generator], out, 10)
                        continue
                    result = core.run(cast(Project, project), cast(Policy, policy), source, [generator], out, 10)
                self.assertEqual(result.score, 11)
                self.assertEqual(result.trials, 4)
                self.assertEqual(
                    calls,
                    [
                        ("start", None),
                        ("loss", ["de"]),
                        ("lopsided", ["de"]),
                        ("lopsided", None),
                        ("better", ["de"]),
                        ("better", None),
                    ],
                )
                self.assertEqual(clock[0] - 3 * 60, 6)
                self.assertEqual(set(result.trial.compares), set(project.versions))
