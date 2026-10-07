"""Real Shape*/Gfx** publication conflict, without changing authored declarations."""

import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from unbake.config import Held
from unbake.fold import callee_contracts
from unbake.layout import redeclarations
from unbake.layout.header_context import Headers

FIXTURE = Path(__file__).parent / "fixtures/battletanx_pointer_contract"
NAME = "func_800F4DFC"
GENERATED = (FIXTURE / "generated.h").read_text()
AUTHORED = (FIXTURE / "menu_render.h").read_text()
RECORD = json.loads((FIXTURE / "record.json").read_text())
ALIASES = {"Gfx": "union Gfx"}
CANDIDATE = redeclarations.catalog(AUTHORED)[NAME]


class CalleeContractTests(unittest.TestCase):
    def plan(self, *, record=None, candidate=CANDIDATE, generated=True):
        path = Path("/include/span/code.h")
        return callee_contracts.plan(
            {path: GENERATED},
            frozenset({path}) if generated else frozenset(),
            {NAME: candidate},
            {NAME: RECORD if record is None else record},
            ALIASES,
            ("us",),
        )

    def test_real_conflict_is_reconciled_as_a_staged_generated_edit(self):
        with self.assertRaisesRegex(Held, "shared conflict"):
            redeclarations.strip("", [GENERATED, "typedef union Gfx Gfx;\n" + AUTHORED])
        edits = self.plan()
        self.assertEqual(len(edits), 1)
        self.assertEqual(edits[0].before, GENERATED)
        self.assertIn(NAME + "(Gfx **", edits[0].after)
        self.assertEqual(edits[0].versions, ("us",))
        self.assertEqual(redeclarations.strip("", [edits[0].after, "typedef union Gfx Gfx;\n" + AUTHORED]), "")

    def test_authored_headers_and_proven_callee_definitions_are_never_rewritten(self):
        self.assertEqual(self.plan(generated=False), [])
        record = copy.deepcopy(RECORD)
        record["provenance"] = [{"kind": "proven", "source": "src/owner.c"}]
        self.assertEqual(self.plan(record=record), [])
        self.assertEqual((FIXTURE / "menu_render.h").read_text(), AUTHORED)

    def test_real_arity_width_return_and_caller_contradictions_remain_holds(self):
        for candidate in (
            CANDIDATE.replace("Gfx **", "float"),
            CANDIDATE.replace("Gfx **", "long long"),
            CANDIDATE.replace("int " + NAME, "void " + NAME),
            CANDIDATE.replace("int " + NAME, "unsigned long long " + NAME),
            CANDIDATE.replace(", int);", ");"),
            CANDIDATE.replace(", int);", ", ...);"),
        ):
            with self.subTest(candidate=candidate):
                self.assertEqual(self.plan(candidate=candidate), [])
        for field, value in (
            ("arity_known", False),
            ("return_known", False),
            ("missing", [{"register": "r4"}]),
            ("conflicts", ["callee input registers differ across versions"]),
            ("registers", ["r4"]),
        ):
            record = copy.deepcopy(RECORD)
            record["abi"][field] = value
            with self.subTest(field=field):
                self.assertEqual(self.plan(record=record), [])
        self.assertEqual(self.plan(record={}), [])

    def test_same_width_scalar_semantics_are_not_reconciled_by_pointer_proof(self):
        self.assertEqual(self.plan(candidate=CANDIDATE.replace(", int", ", unsigned int", 1)), [])

    def test_selected_authored_wrapper_and_its_typedef_provider_travel_together(self):
        with tempfile.TemporaryDirectory(dir=os.environ["TMPDIR"]) as temporary:
            root = Path(temporary)
            include = root / "include"
            include.mkdir()
            database = root / "types.sqlite"
            database.touch()
            generated = include / "span/code.h"
            authored = include / "menu_render.h"
            graphics = include / "gfx.h"
            scalar = include / "types.h"
            contents = {
                generated: GENERATED,
                authored: AUTHORED,
                graphics: "typedef union Gfx Gfx;\n",
                scalar: "typedef unsigned long long u64;\n",
            }
            headers = Headers(contents, root=include)
            project = SimpleNamespace(root=root, include=(include,))
            manifest = {"headers": {"span/code.h": "0" * 64}}
            with (
                patch("unbake.typemap.types_db.path", return_value=database),
                patch("unbake.typemap.types_db.entries", return_value={NAME: RECORD}) as read,
                patch("unbake.layout.index.load", return_value=manifest),
            ):
                context, edits = callee_contracts.reconcile(
                    project,
                    headers,
                    '#include "menu_render.h"\nint menu(void) {return 0;}\n',
                    "menu",
                    ("us",),
                )
            self.assertEqual(len(edits), 1)
            self.assertIn('#include "gfx.h"', edits[0].after)
            self.assertIn(NAME + "(Gfx **", context.texts[generated])
            self.assertEqual(context.texts[authored], AUTHORED)
            self.assertEqual(headers.texts[generated], GENERATED)
            self.assertEqual(read.call_args.args[2], {NAME})

    def test_alternate_unrequested_authored_provider_is_not_selected(self):
        with tempfile.TemporaryDirectory(dir=os.environ["TMPDIR"]) as temporary:
            root = Path(temporary)
            database = root / "types.sqlite"
            database.touch()
            headers = Headers({root / "span/code.h": GENERATED, root / "menu_render.h": AUTHORED}, root=root)
            project = SimpleNamespace(root=root, include=(root,))
            with (
                patch("unbake.typemap.types_db.path", return_value=database),
                patch("unbake.typemap.types_db.entries") as read,
                patch("unbake.layout.index.load", return_value={"headers": {"span/code.h": "0" * 64}}),
            ):
                context, edits = callee_contracts.reconcile(
                    project, headers, "int menu(void) {return 0;}", "menu", ("us",)
                )
            self.assertIs(context, headers)
            self.assertEqual(edits, [])
            read.assert_not_called()
