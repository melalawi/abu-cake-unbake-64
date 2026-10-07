"""Explicit owning changes use real source/native packets and retain known authority."""

import copy
import hashlib
import json
import struct
import subprocess
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tests.kit import TESTS
from unbake.config import Held
from unbake.fold import self_prototype
from unbake.layout import index, redeclarations, split
from unbake.typemap import declaration_evidence, evidence
from unbake.typemap.mips import Analysis
from unbake.work.attempts import Attempt

FIXTURE = (TESTS / "fold/test_owned_contract.py").parents[1] / "fixtures/ragewars_owned_contract"
BE = "func_802BE0C0_de"
FIVE = "func_8025B920_de"
ALIASES = {"s32": "int", "s16": "short", "SlotCC": "struct SlotCC", "RecordD90": "struct RecordD90"}


def packet():
    programs = json.loads((FIXTURE / "machine.json").read_text())["programs"]
    functions = {}
    for name in (BE, FIVE):
        bodies = {}
        for version, row in programs[name].items():
            data = bytes.fromhex(row["hex"])
            bodies[version] = {
                "address": row["address"],
                "target_sha256": row["target_sha256"],
                **Analysis(
                    name,
                    version,
                    row["address"],
                    0,
                    list(struct.unpack(">" + str(len(data) // 4) + "I", data)),
                    {int(k): v for k, v in row["targets"].items()},
                    {},
                ).run(),
            }
        functions[name] = {"versions": bodies}
    abis = evidence.abi(functions)
    return {name: {**row, "abi": abis[name]} for name, row in functions.items()}


class OwnedContractTests(unittest.TestCase):
    def setUp(self):
        self.records = json.loads((FIXTURE / "records.json").read_text())
        self.measured = packet()
        self.path = Path("/project/include/span_1000/owner.h")
        self.versions = tuple(self.records["holders"])
        self.source = (FIXTURE / (BE + ".c")).read_text()
        self.previous = (FIXTURE / "old-owner.c").read_text()
        self.before = self.records["BE0C0_old_header"]

    def attempt(self, name=BE, text=None):
        text = self.source if text is None else text
        return Attempt(
            "",
            name,
            hashlib.sha256(text.encode()).hexdigest(),
            8,
            {v: {"exact": True} for v in self.versions},
            100,
            True,
            0,
            "gcc-2.8.1-sn64",
        )

    def plan(self, *, name=BE, text=None, previous="default", measured=None, header=None, generated=True):
        text = self.source if text is None else text
        project = SimpleNamespace(
            root=Path("/project"),
            cache=None,
            include=(self.path.parent.parent,),
            src=Path("/project/src"),
            versions=self.versions,
        )
        contents = {
            self.path: self.before if header is None else header,
            project.include[0] / "types.h": "typedef int s32; typedef short s16;",
        }
        with (
            patch.object(
                index,
                "load",
                return_value={
                    "headers": {self.path.relative_to(project.include[0]).as_posix(): ""} if generated else {}
                },
            ),
            patch.object(index, "marked", return_value=False),
            patch.object(split, "holding_versions", return_value=self.versions),
            patch.object(
                self_prototype, "previous_source", return_value=self.previous if previous == "default" else previous
            ),
            patch.object(
                self_prototype, "measure_owned", return_value=self.measured[name] if measured is None else measured
            ),
        ):
            contract = self_prototype.plan(project, contents, text, name, self.versions)
        return list(contract.edits) if contract is not None else []

    def test_red_on_baseline_and_ordinary_snapshot_still_keeps_old_owned_authority(self):
        baseline = types.ModuleType("baseline_self_prototype")
        code = subprocess.check_output(["git", "show", "aedd89b6e065:src/unbake/fold/self_prototype.py"], text=True)
        exec(compile(code, "baseline_self_prototype.py", "exec"), baseline.__dict__)
        old_record = {"provenance": [{"kind": "proven", "function": BE}]}
        self.assertEqual(
            baseline.exact(
                {self.path: self.before},
                frozenset({self.path}),
                self.source,
                self.source,
                BE,
                old_record,
                ALIASES,
                self.versions,
                self.attempt(),
            ),
            [],
        )
        project = SimpleNamespace(include=(self.path.parent.parent,))
        snapshot, _ = declaration_evidence.published_snapshot(
            project, contents={self.path: self.before}, sources={Path("/project/src") / (BE + ".c"): self.source}
        )
        self.assertEqual(len(snapshot), 1)
        self.assertIn("(void);", next(iter(snapshot.values())))
        self.assertEqual(len(self.plan()), 1)
        self.assertEqual(self.before, self.records["BE0C0_old_header"])

    def test_explicit_real_empty_owner_stages_one_row_without_writes_and_updates_retention(self):
        original = copy.deepcopy(self.measured)
        with (
            patch.object(Path, "write_text", side_effect=AssertionError("write before proof")),
            patch.object(
                declaration_evidence, "replace_owned_contract", wraps=declaration_evidence.replace_owned_contract
            ) as calls,
        ):
            edits = self.plan()
        self.assertEqual(calls.call_count, 1)
        self.assertEqual(len(edits), 1)
        self.assertEqual(edits[0].before, self.before)
        self.assertEqual(edits[0].versions, self.versions)
        self.assertIn("unsigned char unused_code", edits[0].after)
        self.assertNotIn("published_9a7290e2222f416be16cf4db", edits[0].after)
        self.assertEqual(self.measured, original)
        snapshot, _ = declaration_evidence.published_snapshot(
            SimpleNamespace(include=(self.path.parent.parent,)),
            contents={self.path: edits[0].after},
            sources={Path("/project/src") / (BE + ".c"): self.source},
        )
        self.assertEqual(len(snapshot), 1)
        self.assertNotIn("(void);", next(iter(snapshot.values())))
        self.assertEqual(self.plan(header=edits[0].after), [])

    def test_real_short_formals_and_defined_index_replace_published_caller_contract_without_old_definition(self):
        text = (FIXTURE / (FIVE + ".c")).read_text()
        edits = self.plan(name=FIVE, text=text, previous=None, header=self.records["5B920_old_header"])
        self.assertEqual(len(edits), 1)
        proposed = redeclarations.catalog(edits[0].after)[FIVE]
        self.assertIn("s16 value", proposed)
        self.assertTrue(proposed.startswith("s32 "))
        self.assertEqual(self.measured[FIVE]["abi"]["return_register"], "r2")
        self.assertTrue(self.measured[FIVE]["abi"]["return_known"])
        bad = copy.deepcopy(self.measured[FIVE])
        bad["abi"]["return_known"] = False
        with self.assertRaisesRegex(Held, "word result is not defined"):
            self.plan(name=FIVE, text=text, previous=None, header=self.records["5B920_old_header"], measured=bad)

    def test_fp_pair_stack_variadic_consumed_and_unknown_evidence_remain_guards(self):
        for text in (
            f"void {BE}(float x) {{}}",
            f"void {BE}(double x) {{}}",
            f"void {BE}(long long x) {{}}",
            f"void {BE}(int a,int b,int c,int d,int e) {{}}",
            f"void {BE}(int a,...) {{}}",
            f"int {BE}(void *x, unsigned char y) {{}}",
            "void caller(void) {}",
        ):
            with self.subTest(text=text), self.assertRaises(Held):
                self.plan(text=text)
        for field, value in (
            ("conflicts", ["consumer contradiction"]),
            ("missing", [{"register": "r5"}]),
            ("used_returns", ["r2"]),
            ("unproven_return_reads", ["r2"]),
            ("return_width", 8),
            ("inputs", {self.versions[0]: ["stack16"]}),
        ):
            bad = copy.deepcopy(self.measured[BE])
            bad["abi"][field] = value
            with self.subTest(field=field), self.assertRaises(Held):
                self.plan(measured=bad)
        for change in ("unknown", "target_sha256", "returns"):
            bad = copy.deepcopy(self.measured[BE])
            bad["versions"][self.versions[0]][change] = ["partial"] if change == "unknown" else None
            with self.subTest(change=change), self.assertRaises(Held):
                self.plan(measured=bad)

    def test_independent_or_conflicting_provider_and_caller_only_changes_refuse(self):
        self.assertEqual(self.plan(header=self.before.replace("published declaration", "unowned"), previous=None), [])
        for header, generated in (
            (self.before, False),
            (self.before.replace("published declaration", "declaration evidence"), True),
            (self.before + f"extern int {BE}(void);\n", True),
        ):
            with self.subTest(header=header), self.assertRaises(Held):
                self.plan(header=header, generated=generated, previous=None)
        with self.assertRaisesRegex(Held, "requested public owning function"):
            self.plan(text=f"extern void {BE}(void *, unsigned char); void caller(void) {{}}")

    def test_default_plan_reads_each_definition_once_and_stages_one_row_without_native_work(self):
        with (
            patch.object(
                self_prototype, "definition_prototypes", wraps=self_prototype.definition_prototypes
            ) as definitions,
            patch.object(
                declaration_evidence, "replace_owned_contract", wraps=declaration_evidence.replace_owned_contract
            ) as rows,
            patch("unbake.runner.compile_unit", side_effect=AssertionError("native work during planning")) as native,
            patch.object(Path, "write_text", side_effect=AssertionError("write during planning")),
        ):
            edits = self.plan()
        self.assertEqual((definitions.call_count, rows.call_count, native.call_count, len(edits)), (2, 1, 0, 1))

    def test_real_instruction_work_is_bounded_and_caller_argument_work_is_preserved(self):
        run = Analysis.run
        with patch.object(Analysis, "run", wraps=run, autospec=True) as count:
            # autospec/wraps does not execute an instance method on all Python versions.
            count.side_effect = run
            measured = packet()
        self.assertEqual(count.call_count, 10)
        self.assertEqual(
            sum(len(body["returns"]) for row in measured.values() for body in row["versions"].values()), 10
        )
        programs = json.loads((FIXTURE / "machine.json").read_text())["programs"]
        self.assertEqual(sum(len(bytes.fromhex(row["hex"])) for n in (BE, FIVE) for row in programs[n].values()), 1540)
        callers = programs["func_802BD974_de"]
        self.assertEqual(sum(len(bytes.fromhex(row["hex"])) for row in callers.values()), 5660)
        for version, row in callers.items():
            target = programs[BE][version]["address"]
            self.assertEqual(
                bytes.fromhex(row["hex"]).count((0x0C000000 | ((target >> 2) & 0x3FFFFFF)).to_bytes(4, "big")), 1
            )
        self.assertEqual((FIXTURE / "func_802BD974_de.c").read_text().count("va_arg("), 15)
        self.assertEqual((FIXTURE / "family-caller.c").read_text().count("va_list *__unbake_cursor ="), 15)
