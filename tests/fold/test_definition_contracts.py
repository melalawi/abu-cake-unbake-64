"""Real generated-own-prototype conflicts retain semantic and holder boundaries."""

import copy
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from unbake.fold import self_prototype

FIXTURE = Path(__file__).parents[1] / "fixtures/ragewars_definition_contract"
RECORDS = json.loads((FIXTURE / "records.json").read_text())
GENERATED = (FIXTURE / "generated.h").read_text()
ALIASES = {"s32": "int", "u32": "unsigned int", "s16": "short", "PickupGoalObj8020EF60": "struct PickupGoalObj8020EF60"}
NAME = "func_80212FDC_eu"
CONTEXT = """
typedef int s32;
struct func_8020A028_S3 { char pad0[0x1D8]; void *unk1D8; };
typedef struct func_8020A028_S3 func_8020A028_S3;
struct func_80212828_S2 { char pad0[0x1454]; void *unk1454; };
typedef struct func_80212828_S2 func_80212828_S2;
/* Storage view of this body's two measured stores; complete extent unknown. */
struct MeasuredStores { char pad0[0x220]; s32 unk220; s32 unk224; };
typedef struct MeasuredStores func_80212FBC_S3;
"""


class DefinitionContractTests(unittest.TestCase):
    def plan(self, name=NAME, *, record=None, source=None, generated=True):
        path = Path("include/group.h")
        source = (FIXTURE / (name + ".c")).read_text() if source is None else source
        return self_prototype.inferred(
            {path: GENERATED},
            frozenset({path}) if generated else frozenset(),
            source,
            name,
            RECORDS[name] if record is None else record,
            ALIASES,
            tuple(RECORDS[name]["versions"]),
        )

    def test_real_pointer_definition_replaces_only_its_inferred_generated_entry(self):
        edits = self.plan("func_8020EF80_eu_x")
        self.assertEqual(len(edits), 1)
        self.assertIn("func_8020EF80_eu_x(PickupGoalObj8020EF60 *arg0)", edits[0].after)
        self.assertIn(RECORDS[NAME]["prototype"], edits[0].after)
        self.assertEqual(edits[0].versions, tuple(RECORDS["func_8020EF80_eu_x"]["versions"]))
        self.assertEqual(edits[0].before, GENERATED)
        self.assertEqual((FIXTURE / "generated.h").read_text(), GENERATED)

    def test_real_complete_two_store_body_compiles_with_its_staged_own_entry(self):
        compiler = shutil.which("cc")
        self.assertIsNotNone(compiler)
        source = (FIXTURE / (NAME + ".c")).read_text()
        source = "\n".join(line for line in source.splitlines() if not line.startswith("#include"))
        edits = self.plan()
        self.assertEqual(len(edits), 1)
        self.assertIn("void " + NAME + "(void *object);", edits[0].after)
        self.assertEqual(edits[0].versions, ("eu", "eu-x", "us-rev1"))
        header = "\n".join(line for line in GENERATED.splitlines() if NAME in line)
        staged = "\n".join(line for line in edits[0].after.splitlines() if NAME in line)
        run = subprocess.run
        with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as directory:
            path = Path(directory) / "body.c"
            with patch("subprocess.run", wraps=run) as work:
                path.write_text(CONTEXT + header + source)
                red = subprocess.run(
                    [compiler, "-std=c89", "-Wall", "-Werror", "-fsyntax-only", str(path)],
                    capture_output=True,
                    text=True,
                )
                self.assertNotEqual(red.returncode, 0)
                self.assertIn("conflicting types", red.stderr)
                path.write_text(CONTEXT + staged + source)
                green = subprocess.run(
                    [compiler, "-std=c89", "-Wall", "-Werror", "-fsyntax-only", str(path)],
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(green.returncode, 0, green.stderr)
            self.assertEqual(work.call_count, 2)
        self.assertEqual(source.count("->unk220 = 0"), 1)
        self.assertEqual(source.count("->unk224 = -1"), 1)

    def test_authoritative_c_and_consumed_returns_are_not_discarded(self):
        for kind in ("published", "proven"):
            record = copy.deepcopy(RECORDS[NAME])
            record["provenance"] = [{"kind": kind}]
            with self.subTest(kind=kind):
                self.assertEqual(self.plan(record=record), [])
        for field, value in (
            ("used_returns", ["r2"]),
            ("unproven_return_reads", ["r2"]),
            ("missing", [{"register": "r4"}]),
            ("conflicts", ["entry registers differ"]),
            ("arity_known", False),
        ):
            record = copy.deepcopy(RECORDS[NAME])
            record["abi"][field] = value
            with self.subTest(field=field):
                self.assertEqual(self.plan(record=record), [])
        self.assertEqual(self.plan(generated=False), [])
        # Real published caller claims differ in arity and narrow parameter/
        # return semantics; this unit preserves those genuine contradictions.
        self.assertEqual(self.plan("func_80245B74_de"), [])
        self.assertEqual(self.plan("func_8025B920_de"), [])

    def test_scalar_fp_pair_and_aggregate_transports_do_not_change_to_fit(self):
        source = (FIXTURE / (NAME + ".c")).read_text()
        for type_ in ("float", "double", "long long", "unsigned int", "struct Unknown"):
            changed = source.replace("void *object", type_ + " object")
            with self.subTest(type=type_):
                self.assertEqual(self.plan(source=changed), [])
        self.assertEqual(self.plan(source=source.replace("void *object", "void *object, ...")), [])
