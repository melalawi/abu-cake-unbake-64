"""Authored declaration selection, shared folding and feedback boundaries."""

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from tests.match.support import MatchFixture
from unbake.layout import headers as mapped_headers
from unbake.layout import index as layout_index
from unbake.layout import map as ownership
from unbake.layout.header_context import Headers
from unbake.match import declarations
from unbake.config import Held
from unbake.typemap import declaration_evidence as evidence


class SelectionTests(TestCase):
    def test_selection_table_and_dependency_closure(self):
        old = Path("/old/include")
        root = Path("/live/include")
        project = SimpleNamespace(declaration_evidence=(old,), include=(root,))
        rows = evidence.units(
            {
                old / "shared/session.h": """#ifndef OLD_SESSION_H
#define OLD_SESSION_H
typedef struct Vec3f {float x; float y; float z;} Vec3f;
typedef struct View {Vec3f position;} View;
extern View global;
extern int call(View *p);
#define VALUE 3
#define ACCESS(p) ((View *)(p))->position.x
enum Mode {READY=1, WAITING=2};
#endif
""",
                old / "unrelated.h": (
                    "typedef int Unrelated;\nstatic int implementation(void) {return 1;}\nint initialized = 3;\n"
                ),
            }
        )
        empty = SimpleNamespace(texts={})
        cases = (
            ("type closure", "void alpha(View *p) {p->position.x=0;}", empty, {"View", "Vec3f"}),
            ("data closure", "int alpha(void) {return global.position.x;}", empty, {"global", "View", "Vec3f"}),
            ("prototype closure", "int alpha(void *p) {return call(p);}", empty, {"call", "View", "Vec3f"}),
            (
                "macro closure",
                "int alpha(void *p) {return ACCESS(p)+VALUE;}",
                empty,
                {"ACCESS", "VALUE", "View", "Vec3f"},
            ),
            ("enum", "int alpha(void) {return READY;}", empty, {"READY", "WAITING"}),
            ("local ownership", "typedef int View; int alpha(View p) {return p;}", empty, set()),
            ("comments strings", 'char *alpha(void) {/* View */ return "global VALUE";}', empty, set()),
            (
                "installed",
                "int alpha(void) {return VALUE;}",
                SimpleNamespace(texts={root / "sdk.h": "#define VALUE 3\n"}),
                set(),
            ),
            ("unknown", "void alpha(Missing *p) {}", empty, set()),
            ("condition", "#if VALUE\nint alpha(void) {return 1;}\n#endif", empty, {"VALUE"}),
        )
        with patch.object(evidence, "catalogue", return_value=rows):
            for label, text, headers, expected in cases:
                with self.subTest(label=label):
                    selected = evidence.select(project, headers, text, "alpha")
                    self.assertEqual(set().union(*(unit.names for unit in selected)), expected)
        self.assertFalse(any("initialized" in unit.names or "implementation" in unit.names for unit in rows))
        self.assertEqual(evidence.units({Path("/old.h"): "typedef int M2C_UNK;"}), ())

    def test_full_layout_retains_its_separate_authored_typedef(self):
        project = SimpleNamespace(include=(Path("/live"),), declaration_evidence=())
        for kind in ("struct", "union"):
            alias = f"typedef {kind} Record Record;\n"
            definition = f"{kind} Record {{int value;}};\n"
            rows = evidence.units({Path("/old.h"): alias + definition})
            for use in ("Record *alpha(Record *p) {return p;}", f"{kind} Record *alpha(void);"):
                with self.subTest(kind=kind, use=use), patch.object(evidence, "catalogue", return_value=rows):
                    selected = evidence.select(project, SimpleNamespace(texts={}), use, "alpha")
                    self.assertEqual({unit.text for unit in selected}, {alias, definition})

    def test_ambiguous_declarations_hold_and_duplicate_evidence_reuses(self):
        root = Path("/include")
        project = SimpleNamespace(include=(root,), declaration_evidence=())
        for second, held in (("typedef int T;", False), ("typedef float T;", True)):
            rows = evidence.units({Path("/a.h"): "typedef int T;", Path("/b.h"): second})
            with self.subTest(second=second), patch.object(evidence, "catalogue", return_value=rows):
                if held:
                    with self.assertRaisesRegex(Held, "ambiguous authored declarations"):
                        evidence.select(project, SimpleNamespace(texts={}), "T alpha(T x) {return x;}", "alpha")
                else:
                    self.assertEqual(
                        len(evidence.select(project, SimpleNamespace(texts={}), "T alpha(T x) {return x;}", "alpha")), 1
                    )

    def test_symbol_inventory_table(self):
        project = SimpleNamespace(names_from="us", version=lambda v: SimpleNamespace(symbols=Path("/" + v)))
        cases = (
            ("known data", "extern int global;", {"global": (0x80001000, 0, None)}, None),
            ("known function", "int call(void);", {"call": (0x80001000, 0, None)}, None),
            ("missing data", "extern int global;", {}, "absent live symbol/address inventory"),
            (
                "address conflict",
                "extern int D_80001000;",
                {"D_80001000": (0x80002000, 0, None)},
                "live address disagrees",
            ),
            ("matching address", "extern int D_80001000;", {"D_80001000": (0x80001000, 0, None)}, None),
            ("macro", "#define VALUE 3\n", {}, None),
            (
                "unknown pointer macro",
                "#define memory (*(int *)0x80001000)\n",
                {},
                "absent live symbol/address inventory",
            ),
            ("known pointer macro", "#define memory (*(int *)0x80001000)\n", {"memory": (0x80001000, 0, None)}, None),
            (
                "wrong pointer macro",
                "#define memory (*(int *)0x80002000)\n",
                {"memory": (0x80001000, 0, None)},
                "address-valued macro disagrees",
            ),
        )
        for label, text, symbols, reason in cases:
            with self.subTest(label=label), patch.object(evidence.inventory, "symbols", return_value=("", symbols)):
                selected = evidence.units({Path("/old.h"): text})
                if reason:
                    with self.assertRaisesRegex(Held, reason):
                        evidence.validate_symbols(project, selected, ("us",))
                else:
                    evidence.validate_symbols(project, selected, ("us",))

    def test_address_identity_can_belong_to_another_cartridge_version(self):
        project = SimpleNamespace(
            versions=("us", "eu"), names_from="eu", version=lambda v: SimpleNamespace(symbols=Path("/" + v))
        )
        selected = evidence.units({Path("/old.h"): "extern int D_80001000;"})

        def inventory(path):
            return "", {"D_80001000": (0x80001000 if path.name == "us" else 0x80202000, 0, None)}

        with patch.object(evidence.inventory, "symbols", side_effect=inventory):
            evidence.validate_symbols(project, selected, ("eu",))

    def test_no_evidence_preserves_source_and_performs_no_inventory_read(self):
        project = SimpleNamespace(declaration_evidence=(), include=(Path("/include"),))
        with patch.object(evidence.inventory, "symbols") as symbols:
            self.assertEqual(
                evidence.inject(project, SimpleNamespace(texts={}), "void alpha(void) {}", "alpha", ("us",)),
                ("void alpha(void) {}", 0),
            )
        symbols.assert_not_called()


class SplitEvidenceTests(TestCase):
    def test_evidence_macro_body_closure_enum_constants_and_plain_prototypes(self):
        root = Path("/include")
        marker = "/* unbake declaration evidence: evidence_1234abcd */\n"
        contents = {
            root / "shared/.evidence_type.h": marker + "typedef struct Record {int value;} Record;",
            root / "shared/.evidence_macro.h": marker + "#define ACCESS(p) (((Record *)(p))->value + COUNT)\n",
            root / "shared/.evidence_count.h": marker + "#define COUNT 3\n",
            root / "shared/.evidence_enum.h": marker + "enum Mode {READY=1};\n",
            root / "shared/.evidence_call.h": marker + "int call(Record *);\n",
        }
        layout = mapped_headers.Layout(contents, contents, root, ownership=ownership.Map(2, ()), sources={})
        macro = layout.homes[root / "shared/.evidence_macro.h"]
        layout.headers[macro].decode()
        for dependency in ("type", "count"):
            home = layout.homes[root / ("shared/.evidence_" + dependency + ".h")]
            self.assertEqual(home, macro)
        for name, kind in (("READY", "enum"), ("call", "call"), ("ACCESS", "macro")):
            with self.subTest(name=name):
                required = layout.required(f"int alpha(void) {{return {name}(0);}}")
                self.assertIn(layout.homes[root / ("shared/.evidence_" + kind + ".h")], required)


class FoldTests(MatchFixture):
    def setUp(self):
        super().setUp()
        self.old = self.root.parent / "old/include"
        (self.old / "shared").mkdir(parents=True)
        self.project = replace(self.project, declaration_evidence=(self.old,))

    def test_missing_by_value_dependency_is_folded_to_shared_layouts(self):
        header = self.old / "shared/session.h"
        original = "typedef struct Vec3f {float x; float y; float z;} Vec3f;\n"
        original += "typedef struct View {Vec3f position;} View;\n"
        header.write_text(original)
        body = "int alpha(View *p) {return p->position.x;}\n"
        result = declarations.fold_source(
            self.project,
            self.policy,
            Headers.read(self.project),
            "alpha",
            body,
            ("us", "eu"),
            prove_headers=False,
        )
        generated = "\n".join(edit.after for edit in result.headers)
        self.assertIn("float x;", generated)
        self.assertIn("Vec3f position;", generated)
        self.assertNotIn("typedef struct", result.source)
        self.assertTrue(result.source.endswith(body))
        self.assertNotIn("evidence boundary", result.source)
        self.assertEqual(header.read_text(), original)
        self.assertTrue(all(edit.path.is_relative_to(self.project.include[0]) for edit in result.headers))

    def test_authored_anonymous_vector_and_alias_are_imported_for_by_value_field(self):
        (self.old / "vectors.h").write_text(
            "typedef struct {float x; float y; float z;} Vector3f;\ntypedef Vector3f Vec3f;\n"
        )
        body = "typedef struct View {Vec3f position;} View;\nint alpha(View *p) {return p->position.x;}\n"
        result = declarations.fold_source(
            self.project, self.policy, Headers.read(self.project), "alpha", body, ("us", "eu"), prove_headers=False
        )
        generated = "\n".join(edit.after for edit in result.headers)
        self.assertIn("float x;", generated)
        self.assertIn("Vector3f position;", generated)
        self.assertIn("struct Vector3f", generated)
        self.assertNotIn("Vec3f: missing type layout", result.source)

    def test_array_extent_dependency_uses_authored_macro(self):
        (self.old / "shared/session.h").write_text(
            "#define COUNT 3\ntypedef struct Record {int values[COUNT];} Record;\n"
        )
        body = "int alpha(Record *p) {return p->values[0];}\n"
        result = declarations.fold_source(
            self.project, self.policy, Headers.read(self.project), "alpha", body, ("us", "eu"), prove_headers=False
        )
        generated = "\n".join(edit.after for edit in result.headers)
        self.assertIn("#define COUNT 3", generated)
        self.assertIn("int values[3];", generated)

    def test_supplemental_declarations_use_split_generation_and_feedback(self):
        (self.old / "shared/session.h").write_text("typedef int Word;\nextern Word beta(Word);\n#define VALUE 7\n")
        body = "int alpha(void) {return beta(VALUE);}\n"
        result = declarations.fold_source(
            self.project, self.policy, Headers.read(self.project), "alpha", body, ("us", "eu"), prove_headers=False
        )
        self.assertTrue(result.source.endswith(body))
        self.assertNotIn("#define VALUE", result.source)
        for edit in result.headers:
            edit.path.parent.mkdir(parents=True, exist_ok=True)
            edit.path.write_text(edit.after)
        layout_index.update(self.project, {edit.path: edit.after for edit in result.headers})
        components = evidence.feedback_components(self.project)
        self.assertEqual(len(components), 3)
        self.assertIn("#define VALUE 7", "\n".join(components.values()))
        regenerated = mapped_headers.Layout(
            components, components, self.project.include[0], ownership=ownership.load(self.project), sources={}
        )
        self.assertEqual(set(regenerated.headers), {edit.path for edit in result.headers})
        self.assertEqual(evidence.feedback_components(self.project), components)

    def test_explicit_same_name_layout_uses_existing_collision_rules(self):
        for old_type in ("int", "float"):
            with self.subTest(old_type=old_type):
                (self.project.include[0] / "live.h").write_text("typedef struct Record {int value;} Record;\n")
                (self.old / "shared/session.h").write_text(f"typedef struct Record {{{old_type} value;}} Record;\n")
                body = "int alpha(Record *p) {return p->value;}\n"
                result = declarations.fold_source(
                    self.project,
                    self.policy,
                    Headers.read(self.project),
                    "alpha",
                    f"typedef struct Record {{{old_type} value;}} Record;\n" + body,
                    ("us", "eu"),
                    prove_headers=False,
                )
                generated = "\n".join(edit.after for edit in result.headers)
                if old_type == "int":
                    self.assertFalse(any("struct Record {" in edit.after for edit in result.headers))
                    self.assertIn("live.h", result.source)
                else:
                    self.assertIn("float value;", generated)
                    self.assertNotIn("typedef struct Record {", generated)
                self.assertNotIn("shared/session.h", result.source)

    def test_declaration_admission_ignores_unmatched_body_and_keeps_compatible_aliases(self):
        (self.old / "vectors.h").write_text(
            "typedef struct {float x; float y; float z;} Vector3f;\ntypedef Vector3f Vec3f;\n"
        )
        source = self.root.parent / "alpha.c"
        body = "void alpha(Vec3f *p) {goto *((void **)p);}\n"
        source.write_text(body)
        edits, reports = evidence.plan_many(self.project, self.policy, (source,))
        self.assertEqual(reports[0]["status"], "admitted")
        generated = "\n".join(edit.after for edit in edits)
        self.assertIn("Vec3f", generated)
        self.assertIn("float x;", generated)
        self.assertNotIn("goto", generated)
        self.assertEqual(source.read_text(), body)
        self.assertFalse(any(edit.path.exists() for edit in edits if not edit.before))

    def test_declaration_admission_reports_per_source_refusals_without_installing(self):
        (self.old / "shared/session.h").write_text("extern int missing;\n#define VALUE 7\n")
        paths = []
        for name, body in (("alpha", "int alpha(void) {return missing;}"), ("beta", "int beta(void) {return VALUE;}")):
            source = self.root.parent / (name + ".c")
            source.write_text(body)
            paths.append(source)
        edits, reports = evidence.plan_many(self.project, self.policy, tuple(paths))
        self.assertEqual([report["status"] for report in reports], ["held", "admitted"])
        self.assertIn("absent live symbol/address inventory", reports[0]["reason"])
        self.assertTrue(edits)
        self.assertFalse(any(edit.path.exists() for edit in edits))

    def test_evidence_files_content_change_invalidates_catalogue(self):
        path = self.old / "shared/session.h"
        for text in ("typedef int T;", "typedef float T;"):
            path.write_text(text)
            self.assertEqual(evidence.catalogue(self.project)[0].text.strip(), text)
        (self.old / "m2c_prelude.h").write_text("typedef int M2C_UNK;\n#define NULL 0\n")
        self.assertNotIn(self.old / "m2c_prelude.h", evidence.files(self.project))
        missing = replace(self.project, declaration_evidence=(self.old / "absent",))
        with self.assertRaisesRegex(Held, "include tree missing"):
            evidence.files(missing)


class DatabaseFeedbackTests(TestCase):
    def test_solve_retains_macro_and_prototype_evidence_in_database_and_generated_headers(self):
        import tempfile

        from tests.decomp.support import fixture
        from unbake.typemap import map_program, solve

        with tempfile.TemporaryDirectory() as temporary:
            project, policy, _ = fixture(Path(temporary).resolve(), versions=("us", "eu"), case=self)
            headers = Headers.read(project)
            body = "#define EVIDENCE_VALUE 7\nextern int beta(void);\n"
            source, edits = evidence.promote(
                project,
                headers,
                body
                + "/* unbake declaration evidence boundary */\n"
                + "int alpha(void) {return beta()+EVIDENCE_VALUE;}\n",
                len(body),
            )
            for edit in edits:
                edit.path.parent.mkdir(parents=True, exist_ok=True)
                edit.path.write_text(edit.after)
            layout_index.update(project, {e.path: e.after for e in edits})
            components = evidence.feedback_components(project)
            map_program(project)
            database = solve(project, policy)
            self.assertEqual(
                set(database["declaration_evidence"]),
                {str(path.relative_to(project.include[0])) for path in components},
            )
            self.assertEqual(evidence.feedback_components(project), components)
            self.assertIn("EVIDENCE_VALUE", source)
            self.assertTrue(any("EVIDENCE_VALUE 7" in path.read_text() for path in layout_index.headers(project)))

    def test_solve_keeps_retained_evidence_when_consumer_owns_the_alias(self):
        import tempfile

        from tests.decomp.support import fixture
        from unbake.typemap import map_program, solve, storage

        with tempfile.TemporaryDirectory() as temporary:
            project, policy, _ = fixture(Path(temporary).resolve(), versions=("us", "eu"), case=self)
            (project.src / "alpha.c").write_text("typedef float Word;\nint alpha(void) {Word x=1; return x;}\n")
            body = "typedef int Word;\n"
            _, edits = evidence.promote(
                project,
                Headers.read(project),
                body + "/* unbake declaration evidence boundary */\nvoid beta(void) {}\n",
                1,
            )
            for edit in edits:
                edit.path.parent.mkdir(parents=True, exist_ok=True)
                edit.path.write_text(edit.after)
            layout_index.update(project, {edit.path: edit.after for edit in edits})
            retained = evidence.feedback_components(project)
            map_program(project)
            solve(project, policy)
            self.assertEqual(evidence.feedback_components(project), retained)
            self.assertNotIn("inputs_sha256", storage.read(project.build / "types/database.json", "db"))
            solve(project, policy)
            self.assertEqual(evidence.feedback_components(project), retained)


class SolveAdmissionTests(MatchFixture):
    def test_failed_solve_rolls_back_declaration_admission_edits(self):
        import argparse

        from unbake.cli import solve as command
        from unbake.layout.split import Edit

        path = self.project.include[0] / "shared/evidence.h"
        edits = [Edit(path, "", "typedef int T;\n", self.project.versions)]
        args = argparse.Namespace(declarations_needed=[self.root.parent / "needed.c"])
        with (
            patch.object(evidence, "plan_many", return_value=(edits, [])),
            patch.object(command, "solve", side_effect=Held("solve", "test refusal")),
            self.assertRaisesRegex(Held, "test refusal"),
        ):
            command.run(args, self.project, self.policy)
        self.assertFalse(path.exists())

    def test_successful_solve_publishes_admitted_declarations_at_declared_confidence(self):
        import argparse

        from unbake.cli import solve as command
        from unbake.typemap import map_program

        old = self.root.parent / "old"
        old.mkdir()
        (old / "types.h").write_text("typedef struct NewView {float x; float y; float z;} NewView;\n")
        source = self.root.parent / "alpha.c"
        source.write_text("void alpha(NewView *p) {goto *((void **)p);}\n")
        from tests.decomp.support import fixture

        (self.root.parent / "solve-fixture").mkdir()
        project, policy, _ = fixture(self.root.parent / "solve-fixture", versions=("us", "eu"), case=self)
        project = replace(project, declaration_evidence=(old,))
        map_program(project)
        args = argparse.Namespace(declarations_needed=[source])
        with patch.object(command.common, "receipt", return_value=True), patch.object(command.common, "suggest"):
            command.run(args, project, policy)
        import json

        database = json.loads((project.build / "types/database.json").read_text())
        self.assertIn("NewView", database["structs"])
        self.assertEqual(database["structs"]["NewView"]["state"], "known")
        self.assertFalse((project.build / "types/proven.json").exists())
        self.assertTrue((project.build / "types/declaration-admission.json").exists())
