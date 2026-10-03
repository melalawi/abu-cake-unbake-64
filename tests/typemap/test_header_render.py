"""Generated aggregate spellings keep their typedefs and dependency order."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.decomp.support import fixture
from unbake.project.config import Held
from unbake.typemap import database, declarations
from unbake.typemap.solver import Constraints, _merge_records


class HeaderRenderTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(dir=os.environ["TMPDIR"])
        self.addCleanup(directory.cleanup)
        self.project, self.policy, _ = fixture(Path(directory.name).resolve(), case=self)

    def render(self, source, *, partial=False):
        seed = declarations.extract(source, {})
        value = {kind: {} for kind in ("functions", "globals", "arrays")}
        value["structs"] = {
            name: {**row, "state": "known", "generated": not partial, "partial": partial}
            for name, row in seed["structs"].items()
        }
        return self.capture(value)

    def capture(self, value):
        class Captured(Exception):
            pass

        result = {}

        def capture(project, outputs, *args, **kwargs):
            result.update({path.name: content.decode() for path, content in outputs.items()})
            raise Captured()

        with (
            patch.object(database, "validate_headers", side_effect=capture),
            patch("unbake.typemap.storage.stage_json") as stage,
            self.assertRaises(Captured),
        ):
            database.publish(self.project, value, {}, policy=self.policy)
        stage.assert_not_called()
        return result["typemap.h"]

    def test_generated_alias_and_definition_precede_recursive_header_consumer(self):
        consumer = self.project.include[0] / "consumer.h"
        consumer.write_text(
            '#ifndef CONSUMER_H\n#define CONSUMER_H\n#include "shared/typemap.h"\n'
            "struct Holder { Vector value; };\n#endif\n"
        )
        text = self.render("typedef struct Vector { float x, y, z; } Vector;")
        self.assertLess(text.index("typedef struct Vector Vector;"), text.index("struct Vector {"))
        self.assertLess(text.index("struct Vector {"), text.index('#include "consumer.h"'))
        self.assertEqual(text.count("typedef struct Vector Vector;"), 1)

    def test_partial_union_keeps_multiple_aliases_and_unaliased_struct(self):
        text = self.render(
            "typedef union Payload { int word; float real; } Payload, PayloadAlias;struct Plain { int value; };",
            partial=True,
        )
        self.assertIn("typedef union Payload Payload;", text)
        self.assertIn("typedef union Payload PayloadAlias;", text)
        self.assertIn("union Payload {", text)
        self.assertIn("struct Plain {", text)
        self.assertNotIn("typedef struct Plain", text)

    def test_mutual_pointer_aliases_do_not_create_definition_cycles(self):
        text = self.render("typedef struct A A; typedef struct B B; struct A { B *next; }; struct B { A *next; };")
        for name in ("A", "B"):
            self.assertLess(text.index(f"typedef struct {name} {name};"), text.index("struct A {"))
        parsed = declarations.extract(declarations.clean(text), {})
        self.assertEqual(parsed["structs"]["A"]["size"], 4)
        self.assertEqual(parsed["structs"]["B"]["size"], 4)

    def test_by_value_alias_provider_definition_precedes_consumer(self):
        text = self.render("typedef struct Z Z; struct Z { int value; }; struct A { Z value; };")
        self.assertLess(text.index("struct Z {"), text.index("struct A {"))
        declarations.extract(declarations.clean(text), {})

    def test_missing_array_and_pointer_callback_typedefs_are_ordered_without_cycles(self):
        source = (
            "typedef short State[16]; typedef struct Owner Owner;"
            "typedef void (*Callback)(Owner *);"
            "struct Owner { State samples; Callback callback; };"
        )
        text = self.render(source)
        self.assertLess(text.index("typedef short State[16];"), text.index("struct Owner {"))
        self.assertRegex(text, r"typedef void \(\s*\*Callback\)\(struct Owner \*\);")
        self.assertLess(text.index("struct Owner;"), text.index("Callback)"))
        self.assertLess(text.index("Callback)"), text.index("struct Owner {"))
        parsed = declarations.extract(declarations.clean(text), {})
        self.assertEqual(parsed["structs"]["Owner"]["size"], 36)

    def test_authored_scalar_callback_and_forward_alias_homes_are_reused(self):
        provider = self.project.include[0] / "providers.h"
        provider.write_text("typedef int Word; typedef void (*Callback)(void); typedef struct Owner Owner;\n")
        text = self.render(provider.read_text() + "struct Owner { Word value; Callback callback; };")
        self.assertIn('#include "providers.h"', text)
        self.assertLess(text.index('#include "providers.h"'), text.index("struct Owner {"))
        self.assertNotIn("typedef int Word;", text)
        self.assertNotIn("typedef void (*Callback)", text)
        self.assertNotIn("typedef struct Owner Owner;", text)

    def test_prerequisite_metadata_ignores_values_extents_and_unreferenced_aliases(self):
        seed = declarations.extract(
            "typedef int Word; typedef Word Count; typedef float Unused;struct A { Count Word; int Unused[2]; };",
            {},
        )
        self.assertEqual(seed["structs"]["A"]["typedefs"], {"Count": "int"})

    def test_equivalent_prerequisite_spellings_do_not_create_layout_conflicts(self):
        first = declarations.extract("typedef int Word; struct A { Word value; };", {"version": "us"})
        second = declarations.extract("typedef int Count; struct A { Count value; };", {"version": "eu"})
        graph = Constraints()
        merged = _merge_records([first, second], "structs", graph)
        self.assertFalse(merged["A"]["declaration_conflict"])
        self.assertEqual(merged["A"]["typedefs"], {"Count": "int"})
        self.assertEqual(graph.facts, [])

    def test_callback_prerequisites_resolve_parameters_and_preserve_qualifiers(self):
        seed = declarations.extract(
            "typedef int Word; typedef struct Item { int x; } Item;"
            "typedef const Word *(*Callback)(Item *, Word); struct A { Callback callback; };",
            {},
        )
        self.assertEqual(seed["structs"]["A"]["typedefs"], {"Callback": "const int *( *)(struct Item *, int)"})

    def test_unknown_layouts_do_not_publish_aliases_or_prerequisites(self):
        value = {kind: {} for kind in ("functions", "globals", "arrays")}
        value["structs"] = {
            "Missing": {
                "state": "unknown",
                "generated": True,
                "declaration": "struct Missing { int value; };",
                "aliases": ["MissingAlias"],
                "typedefs": {"Absent": "int"},
            }
        }
        text = self.capture(value)
        self.assertIn("Missing: partial shape", text)
        self.assertNotIn("MissingAlias", text)
        self.assertNotIn("Absent", text)

    def test_conflicting_missing_typedefs_refuse_before_validation_or_writes(self):
        value = {kind: {} for kind in ("functions", "globals", "arrays")}
        value["structs"] = {
            name: {
                "state": "known",
                "generated": True,
                "declaration": f"struct {name} {{ Word value; }};",
                "type": f"struct {name}",
                "typedefs": {"Word": type_},
            }
            for name, type_ in (("A", "int"), ("B", "float"))
        }
        with (
            patch.object(database, "validate_headers") as validate,
            patch("unbake.typemap.storage.stage_json") as stage,
            self.assertRaisesRegex(Held, "types.header_parse: conflicting generated typedef Word"),
        ):
            database.publish(self.project, value, {})
        validate.assert_not_called()
        stage.assert_not_called()

    def importing_source(self, text, *, name="published"):
        path = self.project.src / f"{name}.c"
        path.parent.mkdir(exist_ok=True)
        path.write_text('#include "shared/wrapper.h"\n' + text)
        wrapper = self.project.include[0] / "shared/wrapper.h"
        wrapper.parent.mkdir(exist_ok=True)
        wrapper.write_text('#include "shared/typemap.h"\n')
        return path

    def test_published_forward_aliases_are_replaced_with_tags_in_all_consumers(self):
        self.importing_source("typedef struct CollisionInfo CollisionInfo; struct World;\n")
        source = (
            "typedef struct CollisionInfo CollisionInfo; typedef struct World World;"
            "struct CollisionInfo { int value; }; struct World { CollisionInfo *collision; };"
        )
        text = self.render(source)
        self.assertNotIn("typedef struct CollisionInfo CollisionInfo;", text)
        self.assertNotIn("typedef struct World World;", text)
        self.assertIn("struct CollisionInfo;", text)
        self.assertIn("struct CollisionInfo {", text)
        self.assertIn("struct World {", text)
        self.assertRegex(text, r"struct CollisionInfo \*collision;")
        declarations.extract(declarations.clean(text), {})

    def test_every_importing_source_reserves_names_but_unrelated_and_body_names_do_not(self):
        self.importing_source("typedef struct A A;", name="first")
        self.importing_source("typedef struct B B;", name="second")
        (self.project.src / "unrelated.c").write_text("typedef struct C C;")
        self.importing_source("void f(void) { typedef int C; struct C { int x; }; }", name="body")
        text = self.render(
            "typedef struct A { int x; } A; typedef struct B { int x; } B; typedef struct C { int x; } C;"
        )
        self.assertNotIn("typedef struct A A;", text)
        self.assertNotIn("typedef struct B B;", text)
        self.assertIn("typedef struct C C;", text)

    def test_placeholder_compatibility_header_is_filtered_and_uses_are_concrete(self):
        provider = self.project.include[0] / "compat.h"
        provider.write_text(
            "#ifndef COMPAT_H\n#define COMPAT_H\n"
            "typedef signed char M2C_UNK8; typedef short M2C_UNK16; typedef int M2C_UNK;"
            "typedef int M2C_UNK32; typedef long long M2C_UNK64;\n#endif\n"
        )
        original = provider.read_bytes()
        text = self.render(provider.read_text() + "struct A { M2C_UNK x; M2C_UNK8 a; M2C_UNK16 b; M2C_UNK64 *c; };")
        self.assertNotIn("M2C_UNK", text)
        self.assertNotIn('#include "compat.h"', text)
        parsed = declarations.extract(declarations.clean(text), {})
        self.assertEqual(parsed["structs"]["A"]["size"], 12)
        self.assertEqual(provider.read_bytes(), original)

    def test_placeholder_uses_without_compat_header_never_publish_draft_aliases(self):
        text = self.render("typedef int M2C_UNK; struct A { M2C_UNK *p; M2C_UNK values[2]; };")
        self.assertNotIn("M2C_UNK", text)
        parsed = declarations.extract(declarations.clean(text), {})
        self.assertEqual(parsed["structs"]["A"]["size"], 12)

    def test_source_owned_callbacks_arrays_and_scalars_get_safe_shared_spellings(self):
        self.importing_source("typedef int Word; typedef short State[16]; typedef void (*Handler)(void); ")
        text = self.render(
            "typedef int Word; typedef short State[16]; typedef void (*Handler)(void);"
            "struct A { Word Word; State samples; Handler callback; };"
        )
        self.assertNotIn("typedef int Word;", text)
        self.assertNotIn("typedef short State[16];", text)
        self.assertNotIn("(*Handler)", text)
        self.assertIn("int Word;", text)
        parsed = declarations.extract(declarations.clean(text), {})
        self.assertEqual(parsed["structs"]["A"]["size"], 40)

    def test_authored_mixed_typedefs_retain_unblocked_alias_and_layout(self):
        self.importing_source("typedef struct Record Record;")
        provider = self.project.include[0] / "provider.h"
        provider.write_text("typedef struct Record { int x; } Record, Other; struct User { Record *record; };")
        text = self.render(provider.read_text())
        self.assertNotIn("} Record,", text)
        self.assertIn("} Other;", text)
        self.assertIn("struct Record *record;", text)
        declarations.extract(declarations.clean(text), {})

    def test_prototypes_and_globals_use_tags_and_concrete_placeholders(self):
        self.importing_source("typedef struct Record Record;")
        seed = declarations.extract("typedef struct Record { int x; } Record;", {})
        value = {
            "structs": {name: {**row, "state": "known", "generated": True} for name, row in seed["structs"].items()},
            "functions": {"f": {"state": "known", "prototype": "Record *f(M2C_UNK value);"}},
            "globals": {"g": {"state": "known", "declaration": "extern Record *g;"}},
            "arrays": {},
        }
        outputs = {}

        def capture(project, content, *args, **kwargs):
            outputs.update({p.name: data.decode() for p, data in content.items()})
            raise ValueError("captured")

        with (
            patch.object(database, "validate_headers", side_effect=capture),
            self.assertRaisesRegex(ValueError, "captured"),
        ):
            database.publish(self.project, value, {}, policy=self.policy)
        self.assertIn("extern struct Record *f(s32 value);", outputs["prototypes.h"])
        self.assertIn("extern struct Record *g;", outputs["prototypes.h"])

    def test_conditional_names_after_functions_and_multiline_macros_are_reserved(self):
        self.importing_source(
            "#define BODY(x) \\\n if (x) { }\n"
            "void f(void) {\n#if defined(VERSION_US)\nif (1) {\n#else\nif (0) {\n#endif\n} }\n"
            "typedef struct Later Later;\n"
        )
        from unbake.match import source_views

        # The multiline macro forces the cpp boundary; mock its active-line receipt.
        text = (self.project.src / "published.c").read_text()
        active = set(range(len(text.splitlines()))) - {7}
        with patch.object(source_views, "_preprocessed_lines", return_value=active) as cpp:
            rendered = self.render("typedef struct Later { int x; } Later;")
        cpp.assert_called_once()
        self.assertNotIn("typedef struct Later Later;", rendered)

    def test_source_name_parse_refusal_precedes_any_write(self):
        self.importing_source("typedef struct Broken (")
        with patch.object(database, "validate_headers") as validate, self.assertRaisesRegex(Held, "types.header_parse"):
            self.render("struct A { int x; };")
        validate.assert_not_called()

    def test_filtered_compatibility_headers_cannot_leak_through_transitive_includes(self):
        root = self.project.include[0]
        (root / "nested").mkdir()
        compat = root / "nested/compat.h"
        compat.write_text("#ifndef COMPAT_H\n#define COMPAT_H\ntypedef int M2C_UNK;\n#endif\n")
        (root / "nested/consumer.h").write_text('#include "compat.h"\nstruct A { M2C_UNK value; };')
        text = self.render("struct B { int value; };")
        self.assertNotIn("M2C_UNK", text)
        self.assertNotIn('#include "nested/consumer.h"', text)
        self.assertNotIn('#include "compat.h"', text)
        self.assertIn("struct A { int value; };", text)
        self.assertIn("typedef int M2C_UNK;", compat.read_text())

    def test_unknown_placeholder_use_refuses_before_header_writes(self):
        value = {kind: {} for kind in ("functions", "globals", "arrays")}
        value["structs"] = {
            "A": {
                "state": "known",
                "generated": True,
                "type": "struct A",
                "declaration": "struct A { M2C_UNK128 value; };",
            }
        }
        with (
            patch.object(database, "validate_headers") as validate,
            self.assertRaisesRegex(Held, "missing concrete placeholder type"),
        ):
            database.publish(self.project, value, {}, policy=self.policy)
        validate.assert_not_called()

    def test_source_owned_aliases_in_sizeof_bounds_use_tags(self):
        self.importing_source("typedef struct Record Record;")
        text = self.render("typedef struct Record Record; struct Record { char x; char pad[8 - sizeof(Record *)]; };")
        self.assertIn("sizeof(struct Record *)", text)
        self.assertNotIn("typedef struct Record Record;", text)
        declarations.extract(declarations.clean(text), {})

    def test_abi_validation_and_draft_context_use_the_rendered_shared_spellings(self):
        self.importing_source("typedef struct Record Record;")
        seed = declarations.extract("typedef struct Record { int x; } Record;", {})
        carrier = {
            "prototype": "Record *f(M2C_UNK value);",
            "reasons": ["test"],
            "variants": {"r2": {"prototype": "Record *f(M2C_UNK value);", "reasons": ["test"]}},
        }
        value = {
            "structs": {name: {**row, "state": "known", "generated": True} for name, row in seed["structs"].items()},
            "functions": {"f": {"state": "unknown", "abi_declaration": carrier}},
            "globals": {},
            "arrays": {},
            "unknown": [],
            "conflicts": [],
        }
        with (
            patch.object(database, "validate_headers") as validate,
            patch("unbake.typemap.storage.stage_json", side_effect=ValueError("stop")),
            self.assertRaisesRegex(ValueError, "stop"),
        ):
            database.publish(self.project, value, {}, policy=self.policy)
        self.assertEqual(validate.call_count, 2)
        for call in validate.call_args_list:
            self.assertIn("struct Record *f(s32 value);", call.kwargs["abi_context"])
            self.assertNotIn("M2C_UNK", call.kwargs["abi_context"])
        with patch.object(database, "load", return_value=value):
            context = database.context(self.project)
        self.assertIn("struct Record *f(s32 value);", context)
        self.assertNotIn("M2C_UNK", context)

    def test_generated_complex_alias_names_avoid_published_and_authored_names(self):
        self.importing_source("typedef int UnbakeShared_Handler; typedef void (*Handler)(void);")
        text = self.render("typedef void (*Handler)(void); struct A { Handler callback; };")
        self.assertIn("UnbakeShared_Handler_", text)
        self.assertNotIn("(*Handler)", text)
        declarations.extract(declarations.clean(text), {})

    def test_authored_recursive_imports_are_filtered_without_original_includes(self):
        root = self.project.include[0]
        (root / "first.h").write_text('#ifndef FIRST_H\n#define FIRST_H\n#include "second.h"\n#endif\n')
        (root / "second.h").write_text(
            '#ifndef SECOND_H\n#define SECOND_H\n#include "first.h"\ntypedef int M2C_UNK;\n#endif\n'
        )
        text = self.render("struct A { int x; };")
        self.assertNotIn("M2C_UNK", text)
        self.assertNotIn('#include "first.h"', text)
        self.assertNotIn('#include "second.h"', text)

    def test_generated_prototypes_import_reserves_names_on_first_header_write(self):
        path = self.project.src / "published.c"
        path.parent.mkdir(exist_ok=True)
        path.write_text('#include "shared/prototypes.h"\ntypedef struct Record Record;')
        (self.project.include[0] / "shared/prototypes.h").unlink(missing_ok=True)
        text = self.render("typedef struct Record { int x; } Record;")
        self.assertNotIn("typedef struct Record Record;", text)

    def test_comment_includes_and_nested_function_typedefs_do_not_reserve_names(self):
        path = self.project.src / "unrelated.c"
        path.parent.mkdir(exist_ok=True)
        path.write_text('/* #include "shared/typemap.h" */\ntypedef struct Record Record;')
        text = self.render("typedef struct Record { int x; } Record;")
        self.assertIn("typedef struct Record Record;", text)

    def test_private_callback_alias_avoids_other_generated_aliases(self):
        self.importing_source("typedef void (*Handler)(void);")
        text = self.render(
            "typedef struct UnbakeShared_Handler { int x; } UnbakeShared_Handler; "
            "typedef void (*Handler)(void); struct A { Handler callback; };"
        )
        self.assertIn("UnbakeShared_Handler_", text)
        declarations.extract(declarations.clean(text), {})

    def test_same_source_owned_name_keeps_each_layouts_scalar_and_callback_target(self):
        self.importing_source("typedef int Word; typedef void (*Handler)(int);")
        value = {kind: {} for kind in ("functions", "globals", "arrays", "structs")}
        for name, scalar in (("A", "int"), ("B", "float")):
            seed = declarations.extract(
                f"typedef {scalar} Word; typedef void (*Handler)({scalar}); "
                f"struct {name} {{ Word value; Handler callback; }};",
                {},
            )
            value["structs"].update(
                {key: {**row, "state": "known", "generated": True} for key, row in seed["structs"].items()}
            )
        text = self.capture(value)
        self.assertIn("int value;", text)
        self.assertIn("float value;", text)
        self.assertNotIn("typedef int Word;", text)
        self.assertIn("UnbakeShared_Handler", text)
        self.assertIn("UnbakeShared_Handler_", text)
        parsed = declarations.extract(declarations.clean(text), {})
        self.assertEqual(parsed["structs"]["A"]["size"], 8)
        self.assertEqual(parsed["structs"]["B"]["size"], 8)

    def test_existing_tag_references_are_not_new_source_declarations(self):
        self.importing_source(
            "extern struct Vector *external; void f(struct Vector *argument) { struct Local { int x; }; }"
        )
        text = self.render("typedef struct Vector { int x; } Vector;")
        self.assertIn("typedef struct Vector Vector;", text)
        declarations.extract(declarations.clean(text), {})

    def test_authored_anonymous_layout_gets_a_tag_when_its_alias_is_source_owned(self):
        self.importing_source("typedef struct Record Record;")
        provider = self.project.include[0] / "provider.h"
        provider.write_text("typedef struct { int x; } Record, Other; struct User { Record *record; Other *other; };")
        seed = declarations.extract(provider.read_text(), {})
        value = {kind: {} for kind in ("functions", "globals", "arrays")}
        value["structs"] = {name: {**row, "state": "known"} for name, row in seed["structs"].items()}
        text = self.capture(value)
        self.assertIn("typedef struct Record { int x; } Other;", text)
        self.assertIn("struct Record *record;", text)
        declarations.extract(declarations.clean(text), {})
