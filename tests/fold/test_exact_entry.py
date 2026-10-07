"""Exact real own C repairs inferred entries; native consumers remain a proof boundary."""

import copy
import hashlib
import json
import shutil
import struct
import subprocess
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from tests.fold.test_definition_contracts import CONTEXT
from tests.project_fixture import ProjectCase
from unbake import land
from unbake.config import Held
from unbake.fold import declarations, self_prototype
from unbake.fold.callee_contracts import _signature
from unbake.layout import map as ownership
from unbake.layout import redeclarations
from unbake.process import named
from unbake.typemap import evidence, o32
from unbake.typemap.mips import Analysis
from unbake.work.attempts import Attempt

FIXTURE = Path(__file__).parents[1] / "fixtures/ragewars_exact_entry"
RECORDS = json.loads((FIXTURE / "receipts.json").read_text())
NAME = "func_80212FDC_eu"
ALIASES = {
    "s16": "short",
    "s32": "int",
    "u32": "unsigned int",
    "f32": "float",
    "f64": "double",
    "PickupGoalObj8020EF60": "struct PickupGoalObj8020EF60",
    "Root802131E0": "struct Root802131E0",
    "RecordD90": "struct RecordD90",
}


def receipt(name=NAME):
    row = RECORDS[name]
    return Attempt("", name, row["sha256"], row["bytes"], row["versions"], 100.0, True, 0, row["compiler"])


def source(name=NAME):
    return (FIXTURE / (name + ".c")).read_text()


class ExactEntryTests(ProjectCase):
    def plan(self, name=NAME, *, attempt=None, body=None, record=None, generated=True, header=None):
        text = source(name) if body is None else body
        path = self.project.include[-1] / "span_1000/code_80212C90.h"
        before = "\n".join(row["prototype"] for row in RECORDS.values()) if header is None else header
        return self_prototype.exact(
            {path: before},
            frozenset({path}) if generated else frozenset(),
            text,
            text,
            name,
            {} if record is None else record,
            ALIASES,
            tuple(RECORDS[name]["versions"]),
            receipt(name) if attempt is None else attempt,
        )

    def test_all_eight_actual_matched_bodies_change_only_their_inferred_entry(self):
        for name, row in RECORDS.items():
            with self.subTest(name=name):
                text = source(name)
                self.assertEqual(hashlib.sha256(text.encode()).hexdigest(), row["sha256"])
                with (
                    patch.object(Path, "read_text", side_effect=AssertionError("planner rereads a payload")),
                    patch.object(Path, "write_text", side_effect=AssertionError("planner writes before proof")),
                ):
                    path = self.project.include[-1] / "group.h"
                    before = "\n".join(r["prototype"] for r in RECORDS.values())
                    edits = self_prototype.exact(
                        {path: before},
                        frozenset({path}),
                        text,
                        text,
                        name,
                        {},
                        ALIASES,
                        tuple(row["versions"]),
                        receipt(name),
                    )
                self.assertEqual(len(edits), 1)
                self.assertEqual(edits[0].before, before)
                self.assertEqual(edits[0].versions, tuple(row["versions"]))
                catalog = redeclarations.catalog(edits[0].after)
                self.assertNotEqual(catalog[name], row["prototype"])
                self.assertEqual(
                    {n: p for n, p in catalog.items() if n != name},
                    {n: r["prototype"] for n, r in RECORDS.items() if n != name},
                )

    def test_real_native_payload_bounds_work_and_preserves_consumed_words_and_results(self):
        data = (FIXTURE / "machine.json").read_text()
        run = Analysis.run
        functions = {}
        byte_count = 0
        with (
            patch("json.loads", wraps=json.loads) as parse,
            patch.object(Analysis, "run", autospec=True, side_effect=run) as analyze,
        ):
            fixture = json.loads(data)
            for name, versions in fixture["programs"].items():
                functions[name] = {"versions": {}}
                for version, row in versions.items():
                    binary = bytes.fromhex(row["hex"])
                    byte_count += len(binary)
                    self.assertEqual(hashlib.sha256(binary).hexdigest(), row["target_sha256"])
                    words = [word for (word,) in struct.iter_unpack(">I", binary)]
                    body = Analysis(
                        name,
                        version,
                        row["address"],
                        row["start"],
                        words,
                        {int(a): n for a, n in row["targets"].items()},
                        {},
                        jump_targets={int(a): edges for a, edges in row.get("jump_targets", {}).items()},
                    ).run()
                    functions[name]["versions"][version] = {"address": row["address"], **body}
        self.assertEqual(parse.call_count, 1)
        self.assertEqual(analyze.call_count, fixture["body_count"])
        self.assertEqual(analyze.call_count, 56)
        self.assertEqual(byte_count, fixture["body_bytes"])
        self.assertEqual(byte_count, 21764)
        # The proved C owns the semantic result. B2CF0 also leaves an incidental
        # f0 value at exit; its explicit s32 definition promises r2. No new
        # return is promised for the five explicitly void implementations.
        promised = {n: "r2" for n in RECORDS if source(n).split(n + "(", 1)[0].strip().endswith("s32")}
        abi = evidence.abi(functions, promised)
        self.assertEqual(set(promised), {"func_8020EF80_eu_x", "func_8025B920_de", "func_802B2CF0_de"})
        for name in RECORDS:
            signature = _signature(redeclarations.catalog(self.plan(name)[0].after)[name], ALIASES)
            self.assertIsNotNone(signature)
            self.assertEqual(set(abi[name]["registers"]), o32.argument_words(signature, ALIASES))
            self.assertEqual(abi[name]["missing"], [])
            if name in promised:
                self.assertEqual(abi[name]["return_register"], "r2")
                self.assertTrue(abi[name]["return_known"])

    def test_receipt_pins_source_function_and_every_holding_version(self):
        original = receipt()
        for changed in (
            replace(original, sha256="0" * 64),
            replace(original, function="other"),
            replace(original, versions={"eu": {"exact": True}}),
            replace(original, versions={**original.versions, "eu": {"exact": False}}),
            replace(original, versions={**original.versions, "eu": {"exact": True, "fault": {"key": "compile"}}}),
        ):
            with self.subTest(attempt=changed):
                self.assertEqual(self.plan(attempt=changed), [])
        self.assertEqual(self.plan(body=source() + "\n"), [])

    def test_authored_marked_and_measured_contradictions_are_retained(self):
        self.assertEqual(self.plan(generated=False), [])
        for marker in ("published declaration: published_123", "declaration evidence: evidence_123"):
            self.assertEqual(self.plan(header=f"/* unbake {marker} */\n" + RECORDS[NAME]["prototype"]), [])
        for abi in (
            {"missing": [{"register": "r4"}]},
            {"conflicts": ["entry differs by version"]},
            {"unproven_return_reads": ["r2"]},
            {"used_returns": ["r2"]},
            {"inputs": {v: ["r5"] for v in RECORDS[NAME]["versions"]}},
        ):
            self.assertEqual(self.plan(record={"abi": abi}), [])
        for kind in ("proven", "published", "declared"):
            self.assertEqual(self.plan(record={"provenance": [{"kind": kind, "function": NAME}]}), [])
        row = copy.deepcopy(RECORDS["func_802B2CF0_de"])
        self.assertTrue(row["prototype"].startswith("extern double"))
        self.assertEqual(self.plan("func_802B2CF0_de", record={"abi": {"return_known": False}}), [])

    def test_generated_marker_also_guards_the_older_inference_path(self):
        from tests.fold.test_definition_contracts import RECORDS as inferred_records

        path = self.project.include[-1] / "group.h"
        header = "/* unbake published declaration: published_123 */\n" + RECORDS[NAME]["prototype"]
        self.assertEqual(
            self_prototype.inferred(
                {path: header},
                frozenset({path}),
                source(),
                NAME,
                inferred_records[NAME],
                ALIASES,
                tuple(RECORDS[NAME]["versions"]),
            ),
            [],
        )

    def test_aggregate_variadic_and_changed_fp_entry_are_not_invented(self):
        original = source()
        for formal in ("struct Unsupported object", "float object", "double object", "void *object, ..."):
            text = original.replace("void *object", formal)
            attempt = replace(receipt(), sha256=hashlib.sha256(text.encode()).hexdigest())
            self.assertEqual(self.plan(body=text, attempt=attempt), [])

    def test_complete_actual_two_store_body_compiles_with_staged_entry(self):
        compiler = shutil.which("cc")
        self.assertIsNotNone(compiler)
        text = "\n".join(line for line in source().splitlines() if not line.startswith("#include"))
        edits = self.plan()
        self.assertEqual(len(edits), 1)
        output = self.root / "native.c"
        run = subprocess.run
        with patch("subprocess.run", wraps=run) as calls:
            for header, expected in (
                (RECORDS[NAME]["prototype"], False),
                (redeclarations.catalog(edits[0].after)[NAME], True),
            ):
                output.write_text(CONTEXT + header + "\n" + text)
                result = subprocess.run(
                    [compiler, "-std=c89", "-Werror", "-fsyntax-only", str(output)], capture_output=True, text=True
                )
                self.assertEqual(result.returncode == 0, expected, result.stderr)
        self.assertEqual(calls.call_count, 2)
        self.assertEqual(text.count("->unk220 = 0"), 1)
        self.assertEqual(text.count("->unk224 = -1"), 1)


class PublicEntryFoldTests(ProjectCase):
    versions = ("eu", "eu-x", "us-rev1")

    def setUp(self):
        super().setUp()
        (self.project.root / "layout.toml").write_bytes(
            ownership.encoded(
                ownership.Map(
                    2,
                    (
                        ownership.Group("code_80212C90", "span_1000", "default", (NAME,)),
                        ownership.Group("beta", "span_1000", "default", ("beta",)),
                        ownership.Group("gamma", "span_1000", "default", ("gamma",)),
                    ),
                )
            )
        )
        for version in self.versions:
            split = self.project.version(version).split
            split.write_text(
                split.read_text().replace("asm, alpha", "asm, " + NAME).replace("name: main", "name: span_1000")
            )
        self.path = self.project.include[-1] / "span_1000/code_80212C90.h"
        self.path.parent.mkdir()
        self.before = "#ifndef UNBAKE_SPAN_1000_CODE_80212C90_H\n#define UNBAKE_SPAN_1000_CODE_80212C90_H\n"
        self.before += RECORDS[NAME]["prototype"] + "\n#endif\n"
        self.path.write_text(self.before)
        (self.project.include[-1] / "types.h").write_text(CONTEXT)

    def test_public_fold_uses_real_receipt_without_disposable_database_or_index(self):
        self.assertFalse((self.project.build / "types.sqlite").exists())
        self.assertFalse((self.project.build / "layout/index.json").exists())
        with (
            patch("unbake.layout.entries.owners", return_value=[]),
            patch("unbake.layout.structs_fold._prove_includers") as consumers,
            patch.object(self_prototype, "exact", wraps=self_prototype.exact) as reconcile,
        ):
            edits = declarations.folded_edits(
                self.project, self.host, NAME, source(), self.versions, exact_entry=receipt()
            )
        headers = [edit for edit in edits if edit.path == self.path]
        self.assertEqual(len(headers), 1)
        self.assertIn("void " + NAME + "(void *object);", headers[0].after)
        self.assertEqual(headers[0].before, self.before)
        self.assertEqual(reconcile.call_count, 1)
        self.assertEqual(consumers.call_count, 1)
        self.assertEqual(self.path.read_text(), self.before)
        body = next(edit.after for edit in edits if edit.path.suffix == ".c")
        self.assertEqual(body.count("->unk220 = 0"), 1)
        self.assertEqual(body.count("->unk224 = -1"), 1)

    def test_changed_entry_requires_one_strict_consumer_proof_and_preserves_failure(self):
        staged = self.before.replace("extern int " + NAME, "extern void " + NAME)
        for fail in (False, True):
            with (
                self.subTest(fail=fail),
                patch("unbake.land._prove_versions", return_value=set()) as own,
                patch(
                    "unbake.layout.header_step.validate",
                    side_effect=Held(
                        named(
                            "headers.nonregression",
                            "headers.nonregression: consumer uses incompatible result",
                            owner="fixture",
                            stage="headers",
                        )
                    )
                    if fail
                    else None,
                ) as consumers,
            ):
                if fail:
                    with self.assertRaisesRegex(Held, "incompatible result"):
                        land.prove(
                            self.project,
                            self.host,
                            NAME,
                            source(),
                            {"span_1000/code_80212C90.h": staged},
                            self.root / "stage-fail",
                            versions=self.versions,
                        )
                else:
                    land.prove(
                        self.project,
                        self.host,
                        NAME,
                        source(),
                        {"span_1000/code_80212C90.h": staged},
                        self.root / "stage",
                        versions=self.versions,
                    )
                self.assertEqual(own.call_count, 1)
                self.assertEqual(consumers.call_count, 1)
                self.assertTrue(consumers.call_args.kwargs["prove_all"])
                self.assertEqual(consumers.call_args.kwargs["preproved"], frozenset({NAME}))
        self.assertEqual(self.path.read_text(), self.before)
