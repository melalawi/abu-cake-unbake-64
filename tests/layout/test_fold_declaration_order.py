"""Public folded WeaponMenuSetup requires both the complete Menu tag and its late typedef."""

import hashlib
import json
import shutil
import subprocess
from pathlib import Path
from unittest.mock import patch

from tests.project_fixture import ProjectCase
from unbake import cache
from unbake.config import Held
from unbake.layout import structs_fold
from unbake.layout.header_context import Headers
from unbake.layout.shared import append
from unbake.project import headers as header_graph

FIXTURE = Path(__file__).parent / "fixtures/ragewars_weapon_menu"
OWNER = "span_1000/code_80217388.h"


class FoldDeclarationOrderTests(ProjectCase):
    versions = ("de", "eu", "eu_x", "us", "us_rev1")

    def setUp(self):
        super().setUp()
        self.cpp, self.cc = shutil.which("cpp"), shutil.which("cc")
        if self.cpp is None or self.cc is None:
            self.skipTest("native cpp and C compiler required")
        self.include = self.project.include[0]
        self.owner = self.include / OWNER
        self.owner.parent.mkdir(parents=True)
        self.owner.write_bytes((FIXTURE / "include" / OWNER).read_bytes())
        (self.include / "types.h").write_bytes((FIXTURE / "include/types.h").read_bytes())
        self.source = self.project.src / "func_80217388_de.c"
        self.source.write_bytes((FIXTURE / self.source.name).read_bytes())
        self.before = self.owner.read_text()
        self.headers = Headers.read(self.project)
        self.declaration = (FIXTURE / "WeaponMenuSetup.h").read_text()
        _, self.records = self.headers.parse(self.declaration)
        self.native_calls = []

    def native(self, include, version, non_matching):
        options = ["-P", "-I" + str(include), "-DVERSION_" + version.upper(), f"-DNON_MATCHING={non_matching}"]
        expanded = subprocess.run([self.cpp, *options, str(self.source)], capture_output=True, text=True)
        self.native_calls.append(("cpp", version, non_matching))
        self.assertEqual(expanded.returncode, 0, expanded.stderr)
        compiled = subprocess.run(
            [self.cc, "-std=gnu89", "-fsyntax-only", "-x", "c", "-"],
            input=expanded.stdout,
            capture_output=True,
            text=True,
        )
        self.native_calls.append(("cc", version, non_matching))
        self.assertEqual(compiled.returncode, 0, compiled.stderr)
        self.assertLess(
            expanded.stdout.index("typedef struct Menu Menu;"), expanded.stdout.index("struct WeaponMenuSetup {")
        )

    def compile_overlay(self, project, edits, policy, republished):
        self.assertIs(project, self.project)
        self.assertIs(policy, self.host)
        self.assertIsNone(republished)
        include = self.root / "proof/include"
        for path, text in self.headers.texts.items():
            destination = include / path.relative_to(self.include)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(text)
        for edit in edits:
            (include / edit.path.relative_to(self.include)).write_text(edit.after)
        for version in project.versions:
            for non_matching in (0, 1):
                self.native(include, version, non_matching)

    def test_exact_authored_source_and_provider_slice_digests(self):
        provenance = json.loads((FIXTURE / "provenance.json").read_text())
        for relative, digest in provenance["files"].items():
            self.assertEqual(hashlib.sha256((FIXTURE / relative).read_bytes()).hexdigest(), digest)
        self.assertEqual(
            hashlib.sha256((FIXTURE / "func_802181FC_de.c").read_bytes()).hexdigest(), provenance["source_sha256"]
        )

    def test_public_fold_proves_the_actual_consumer_after_both_menu_providers(self):
        original = {path: path.read_bytes() for path in (*self.headers.texts, self.source)}
        with (
            patch.object(structs_fold, "_compile_includers", side_effect=self.compile_overlay) as proof,
            patch.object(self.headers, "parse", wraps=self.headers.parse) as parse,
        ):
            edits = structs_fold.fold(
                self.records, self.project, destination=self.owner, context=self.headers, host=self.host
            )
        self.assertEqual(proof.call_count, 1)
        self.assertEqual(parse.call_count, 3)
        self.assertEqual(len(edits), 1)
        self.assertEqual(edits[0].path, self.owner)
        after = edits[0].after
        self.assertLess(after.index("struct Menu {"), after.index("struct WeaponMenuSetup {"))
        self.assertLess(after.index("typedef struct Menu Menu;"), after.index("struct WeaponMenuSetup {"))
        self.assertEqual(after.count("typedef struct Menu Menu;"), 1)
        self.assertEqual(after.count("typedef struct WeaponMenuSetup WeaponMenuSetup;"), 1)
        self.assertEqual(after.count("struct WeaponMenuSetup {"), 1)
        self.assertEqual(
            [line for line in after.splitlines() if line.startswith("#")],
            [line for line in self.before.splitlines() if line.startswith("#") and line != "#endif"]
            + ['#include "types.h"', "#endif"],
        )
        self.assertEqual(
            self.native_calls,
            [(tool, version, mode) for version in self.versions for mode in (0, 1) for tool in ("cpp", "cc")],
        )
        self.assertEqual({path: path.read_bytes() for path in original}, original)

    def test_proposed_order_projects_each_declaration_once_and_keeps_existing_bytes(self):
        proposed = append(self.before, self.declaration)
        cache.forget()
        with (
            patch.object(self.headers, "parse", wraps=self.headers.parse) as parse,
            patch.object(
                header_graph.Graph, "projection", autospec=True, side_effect=header_graph.Graph.projection
            ) as projection,
            patch.object(header_graph, "scan", wraps=header_graph.scan) as scan,
        ):
            after = structs_fold._order_header(proposed, self.headers, self.before)
        self.assertEqual(parse.call_count, 2)
        # Four existing definitions, four existing typedefs, one new combined typedef/definition.
        self.assertEqual(projection.call_count, 9)
        self.assertEqual(scan.call_count, 9)
        self.assertEqual(sum(len(header_graph.scan(call.args[0])) for call in scan.call_args_list), 0)
        self.assertLess(after.index("typedef struct Menu Menu;"), after.index("typedef struct WeaponMenuSetup {"))
        self.assertEqual(
            after.replace(self.declaration.strip() + "\n", ""), proposed.replace(self.declaration.strip(), "")
        )

    def test_missing_ordinary_menu_provider_is_refused_before_header_proof(self):
        missing = self.before.replace("typedef struct Menu Menu;", "")
        headers = Headers({**self.headers.texts, self.owner: missing}, root=self.project.root)
        with (
            patch.object(structs_fold, "_compile_includers") as proof,
            self.assertRaisesRegex(Held, "Menu"),
        ):
            structs_fold.fold(self.records, self.project, destination=self.owner, context=headers, host=self.host)
        self.assertEqual(proof.call_count, 0)

    def test_conflicting_menu_field_contract_is_refused_before_header_proof(self):
        declaration = self.before[
            self.before.index("struct Menu {") : self.before.index("};", self.before.index("struct Menu {")) + 2
        ]
        _, records = self.headers.parse(declaration.replace("s32 active;", "f32 active;"))
        with (
            patch.object(structs_fold, "_compile_includers") as proof,
            self.assertRaises(Held),
        ):
            structs_fold.fold(records, self.project, destination=self.owner, context=self.headers, host=self.host)
        self.assertEqual(proof.call_count, 0)

    def test_late_typedef_cannot_cross_an_existing_consumer_that_needs_the_addition(self):
        extra = self.include / "setup.h"
        seeded = Headers({**self.headers.texts, extra: self.declaration}, root=self.project.root)
        proposed = append(self.before, self.declaration).replace("void * unkD0;", "WeaponMenuSetup setup;")
        with self.assertRaisesRegex(Held, "cyclic shared-header dependency"):
            structs_fold._order_header(proposed, seeded, self.before)
