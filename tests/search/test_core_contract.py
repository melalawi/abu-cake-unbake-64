"""Shared evidence, staged resolution and measured search contracts."""

import hashlib
import json
import tempfile
import unittest
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tests.match import test_match
from tests.support import tool
from unbake.cli.decomp import run
from unbake.cli.main import make_parser
from unbake.decomp import needs, score, trial
from unbake.decomp.drafts import Store
from unbake.decomp.trial_compare import TYPES, Compare
from unbake.families import Family, family_for
from unbake.layout.split import Edit
from unbake.project.config import Compiler, Held, Policy, Project, Version
from unbake.search import core, methods, register


class CoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.enterContext(patch.object(test_match.match.features, "load"))
        self.enterContext(patch.dict(needs.RESOLVERS, {}, clear=True))
        derivers: list[needs.Deriver] = []
        self.enterContext(patch.object(needs, "derivers", return_value=derivers))
        self.enterContext(patch.object(needs, "register_deriver", side_effect=derivers.append))

    def test_evidence_roundtrip_and_named_refusals(self) -> None:
        cases = [
            needs.SymbolNeed("us", "D_800C7C94", 0x800C7C94, -32768, ".data", "f32", 4, "3c01800d"),
            needs.LabelNeed("us", "inner", 0x80001004, "data", "3f800000"),
            needs.LayoutNeed("us", "Player", [["team", 3, "u8", 1]], "function.c", "offset"),
            needs.RodataNeed("us", ".rdata", "literals", 0x80002000, 4, "3f800000"),
            needs.PlacementNeed("us", "callee", 0x1000, 0x1010, "cut", "asm", "03e00008"),
            needs.GuardFinding("inline-asm", 2, "asm", None),
        ]
        for need in cases:
            with self.subTest(kind=type(need).__name__):
                self.assertEqual(needs.decode(json.loads(json.dumps(needs.encode(need)))), need)
                with self.assertRaisesRegex(Held, needs.name(need)):
                    needs.resolve([need], None, None, lambda *args: self.fail("write before refusal"))
                row = needs.encode(need)
                field = next(key for key in row if key != "need_type")
                del row[field]
                with self.assertRaisesRegex(Held, field):
                    needs.decode(row)
        context = object()
        needs.register_deriver(lambda ctx: cases if ctx is context else [])
        self.assertEqual(needs.derive(context), cases)

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
                family_for(value)

    def test_match_resolves_in_stage_and_refuses_unknown_before_build(self) -> None:
        for unresolved in (False, True):
            with self.subTest(unresolved=unresolved):
                helper = test_match.MatchTests("runTest")
                helper.setUp()
                try:
                    pending = [
                        needs.LabelNeed("us", "inner", 0x80001004, "data", "word"),
                        needs.SymbolNeed("us", "literal", 0x80002000, 0, ".data", "f32", 4, "word"),
                    ]
                    if unresolved:
                        pending.append(needs.RodataNeed("us", "unresolved_pool", "literals", 0x80003000, 4, "word"))
                    source = helper.draft("alpha", pending=pending)
                    calls = []

                    def resolver(
                        batch: list[needs.Need],
                        project: Project,
                        policy: Policy,
                        helper: test_match.MatchTests = helper,
                        calls: list[str] = calls,
                    ) -> list[Edit]:
                        self.assertNotEqual(project.root, helper.project.root)
                        path = project.version("us").symbols
                        before = path.read_text()
                        if isinstance(batch[0], needs.LabelNeed):
                            self.assertIn("literal = ", before)
                        calls.append(type(batch[0]).__name__)
                        return [Edit(path, before, before + f"{needs.name(batch[0])} = 0x80002000;\n", ("us",))]

                    with patch.dict(needs.RESOLVERS, {}, clear=True):
                        needs.register_resolver(needs.LabelNeed, 20, resolver)
                        needs.register_resolver(needs.SymbolNeed, 10, resolver)
                        test_match.match.submit(helper.project, helper.policy, source)
                        receipts = test_match.match.run(helper.project, helper.policy)
                    if unresolved:
                        self.assertEqual(helper.calls, [])
                        self.assertEqual(calls, [])
                        self.assertTrue(any("unresolved_pool" in receipt for receipt in receipts))
                        helper.assert_untouched()
                    else:
                        self.assertEqual(calls, ["SymbolNeed", "LabelNeed"], receipts)
                        self.assertTrue(any("resolved need literal" in receipt for receipt in receipts))
                        self.assertIn("inner = ", helper.project.version("us").symbols.read_text())
                finally:
                    helper.doCleanups()

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
        project = SimpleNamespace(
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
        policy = SimpleNamespace(search_beam=2, stall_trials=2, state_root=self.root / "state")
        source = self.root / "func_8041F2A0.c"
        source.write_text("start")
        calls = []
        words = bytes.fromhex("03e00008000000003c01800d")

        def compile_trial(project: Project, policy: Policy, path: Path, scratch: Path) -> trial.Trial:
            content = path.read_text()
            calls.append(content)
            if content == "invalid":
                raise Held("try", "compile source invalid")
            counts = {"start": (293, 293), "lopsided": (294, 292), "better": (294, 294)}[content]
            comparisons = {}
            for version, count in zip(project.versions, counts, strict=False):
                directory = scratch / version
                directory.mkdir(parents=True)
                (directory / "trial.elf").write_bytes(b"ELF")
                for name in ("baserom", "draft"):
                    (directory / (name + ".bin")).write_bytes(words)
                typed = dict.fromkeys(TYPES, 0)
                typed["changed"] = 294 - count
                comparisons[version] = Compare(version, count, 294, typed, [])
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
                patch.object(core.explain, "allocation", return_value=SimpleNamespace(differences=[], pseudos=[])),
            ):
                run(args, project, policy)
            rows = [json.loads(line) for line in (self.root / "out" / "steps.jsonl").read_text().splitlines()]
            self.assertEqual([row["score"] for row in rows], [293, 293, None, 292, 294])
            self.assertTrue(rows[1]["cached"])
            self.assertEqual(calls.count("start"), 1)
            stored = Store(policy, project).rows(source.stem)
            self.assertEqual(len(stored), 3)
            self.assertTrue(stored[-1]["identical_everywhere"])
            with self.assertRaisesRegex(Held, "unknown"):
                methods("unknown")
        for attribute in ("search_beam", "stall_trials"):
            with self.subTest(missing=attribute):
                incomplete = SimpleNamespace(**{key: value for key, value in vars(policy).items() if key != attribute})
                with self.assertRaisesRegex(Held, attribute):
                    core.run(project, incomplete, source, [Proposals()], self.root / "refused", 5)
