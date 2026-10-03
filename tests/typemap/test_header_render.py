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
