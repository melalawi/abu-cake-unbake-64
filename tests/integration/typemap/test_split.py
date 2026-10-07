"""Shared type emission and consumer dependency contracts use mocked tools."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.decomp.support import fixture
from tests.typemap.split_support import expanded
from unbake.config import Held
from unbake.layout.headers import Layout
from unbake.layout.map import Map
from unbake.typemap import database, declarations, header_names
from unbake.typemap.split import statements


class SplitTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(dir=os.environ["TMPDIR"])
        self.addCleanup(directory.cleanup)
        self.project, self.policy, _ = fixture(Path(directory.name).resolve(), case=self)
        self.root = self.project.include[0]

    def layout(self, texts):
        components = {self.root / name: text for name, text in texts.items()}
        return Layout(components, components, self.root, ownership=Map(2, ()), sources={})

    def test_owned_alias_does_not_expand_into_unrelated_provider(self):
        layout = self.layout(
            {"alias.h": "typedef struct Foreign Callback;", "foreign.h": "struct Foreign {int value;};"}
        )
        for blocked, tags, expected in (
            ({"Callback"}, set(), set()),
            (set(), set(), {self.root / "common/unused.h"}),
            ({"Callback"}, {"Foreign"}, set()),
        ):
            with self.subTest(blocked=blocked, tags=tags):
                self.assertEqual(layout.required("Callback handler;", blocked=blocked, blocked_tags=tags), expected)
        # Explicit tag uses still require their definition even when a typedef is local.
        self.assertEqual(
            layout.required("struct Foreign *p;", blocked={"Callback"}), {layout.homes[self.root / "foreign.h"]}
        )

    def test_owned_callback_does_not_select_same_named_aggregate_tag(self):
        layout = self.layout({"callback.h": "typedef struct Callback Callback; struct Callback {int value;};"})
        self.assertEqual(layout.required("Callback handler;", blocked={"Callback"}), set())
        self.assertEqual(layout.required("struct Callback *p;", blocked={"Callback"}), {self.root / "common/unused.h"})

    def test_callback_collision_names_are_deterministic_and_avoid_existing_providers(self):
        for shared, expected in (
            ({"Callback": "int (*)(int)"}, {}),
            ({"Callback": "float (*)(int)"}, {"Callback": "Callback_owner"}),
            ({"Callback": "struct Callback", "Callback_owner": "int"}, {"Callback": "Callback_owner_"}),
        ):
            with self.subTest(shared=shared):
                self.assertEqual(header_names.callback_renames({"Callback": "int (*)(int)"}, shared, "owner"), expected)

    def test_callback_compatibility_uses_structure(self):
        rows = (
            ("s32 (*)(s32 value, void *p)", "signed int (*)(signed int, void *)", True),
            ("Time (*)(u8 kind, f32 *value)", "int (*)(unsigned char, float *)", True),
            ("void (*)(void (*notify)(int named))", "void (*)(void (*)(int))", True),
            ("void (*)(int values[4])", "void (*)(int *)", True),
            ("void (*)(const int value)", "void (*)(int)", True),
            ("void (*)(PayloadPointer)", "void (*)(void *)", True),
            ("PayloadPointer (*)(int)", "void *(*)(int)", True),
            ("void (*)(ConstWord)", "void (*)(int)", True),
            ("void (*)(ConstWord *)", "void (*)(const int *)", True),
            ("void (*)(ConstWord *)", "void (*)(int *)", False),
            ("void (*)(const int *)", "void (*)(int *)", False),
            ("void (*)(int, ...)", "void (*)(int)", False),
            ("void (*)(void)", "void (*)(int)", False),
            ("int (*)(int)", "unsigned int (*)(int)", False),
            ("void (*)(struct One *)", "void (*)(struct Two *)", False),
            ("void (*)(void)", "struct Callback", False),
        )
        aliases = {
            "s32": "signed int",
            "Time": "s32",
            "u8": "unsigned char",
            "f32": "float",
            "PayloadPointer": "void *",
            "ConstWord": "const s32",
        }
        for left, right, compatible in rows:
            with self.subTest(left=left, right=right):
                self.assertEqual(
                    header_names.type_identity(left, aliases) == header_names.type_identity(right, aliases), compatible
                )

    def test_transitive_closure_excludes_unused_layout_and_orders_value_dependencies(self):
        layout = self.layout(
            {
                "word.h": "typedef int Word;",
                "a.h": "struct A { Word value; };",
                "b.h": "struct B { struct A value; };",
                "unused.h": "struct Unused { double huge[100]; };",
            }
        )
        self.root / "shared/decls/f.h"
        outputs = {p.relative_to(self.root).as_posix(): data.decode() for p, data in layout.headers.items()}
        outputs["shared/decls/f.h"] = (
            "\n".join(layout.include(home) for home in sorted(layout.required("extern struct B *f(void);")))
            + "\nextern struct B *f(void);"
        )
        text = expanded(outputs, "shared/decls/f.h")
        self.assertLess(text.index("typedef int Word"), text.index("struct A {"))
        self.assertLess(text.index("struct A {"), text.index("struct B {"))
        self.assertIn("struct Unused", text)
        declarations.extract(declarations.clean(text), {})

    def test_mutual_tag_pointers_share_cluster_with_forward_declarations(self):
        layout = self.layout({"a.h": "struct A { struct B *next; };", "b.h": "struct B { struct A *next; };"})
        self.assertEqual(len(layout.headers), 1)
        text = next(iter(layout.headers.values())).decode()
        for tag in ("A", "B"):
            self.assertLess(text.index(f"struct {tag};"), text.index("struct A {"))
        declarations.extract(declarations.clean(text), {})

    def test_alias_pointer_cycles_include_both_definitions_for_chained_body_access(self):
        layout = self.layout(
            {
                "aliases.h": "typedef struct A A; typedef struct B B;",
                "a.h": "struct A { B *next; };",
                "b.h": "struct B { A *next; int value; };",
            }
        )
        outputs = {p.relative_to(self.root).as_posix(): data.decode() for p, data in layout.headers.items()}
        self.root / "consumer.h"
        outputs["consumer.h"] = (
            "\n".join(layout.include(home) for home in sorted(layout.required("extern A *head;"))) + "\nextern A *head;"
        )
        text = expanded(outputs, "consumer.h")
        self.assertIn("struct A {", text)
        self.assertIn("struct B {", text)
        declarations.extract(declarations.clean(text), {})

    def test_callback_cycle_has_file_scope_tag_and_callback_before_layout(self):
        layout = self.layout(
            {"callback.h": "typedef void (*Callback)(struct A *);", "a.h": "struct A { Callback cb; };"}
        )
        self.assertEqual(len(layout.headers), 1)
        text = next(iter(layout.headers.values())).decode()
        self.assertLess(text.index("struct A;"), text.index("(*Callback)"))
        self.assertLess(text.index("(*Callback)"), text.index("struct A {"))
        declarations.extract(declarations.clean(text), {})

    def test_invalid_by_value_cycle_refuses(self):
        with self.assertRaisesRegex(Held, "cyclic shared type context"):
            self.layout({"a.h": "struct A { struct B value; };", "b.h": "struct B { struct A value; };"})

    def test_legacy_statement_boundaries_keep_nested_aggregates_and_conditionals(self):
        pieces = statements(
            "struct A { union { int x; float y; } u; };\n#if X\ntypedef int V;\n"
            "#else\ntypedef float V;\n#endif\ntypedef int W;"
        )
        self.assertEqual(len(pieces), 3)
        self.assertIn("float y;", pieces[0])
        self.assertIn("#else", pieces[1])
        with self.assertRaisesRegex(Held, "incomplete"):
            statements("struct Broken {")

    def test_callback_used_by_global_survives_solve_and_emission(self):
        from tests.typemap.test_solver import solve

        value = solve(
            {}, "typedef int (*Handler802A2B50)(void *, int, int, int, int); extern Handler802A2B50 callback;"
        )
        self.assertIn("Handler802A2B50", value["typedefs"])
        outputs = {}

        def capture(project, content, *args, **kwargs):
            outputs.update(
                {p.relative_to(self.root).as_posix(): data.decode() for p, data in content.items() if p.suffix == ".h"}
            )
            raise ValueError("captured")

        with (
            patch.object(database, "validate_headers", side_effect=capture),
            self.assertRaisesRegex(ValueError, "captured"),
        ):
            database.publish(self.project, value, {}, policy=self.policy)
        home = next(name for name in outputs if "Handler802A2B50" in outputs[name])
        text = expanded(outputs, home)
        self.assertIn("Handler802A2B50", text)
        declarations.extract(declarations.clean(text) + "\nextern Handler802A2B50 other;", {})

    def test_sizeof_typedef_dependencies_are_closed(self):
        layout = self.layout(
            {"desc.h": "typedef struct Desc Desc;", "holder.h": "struct Holder { char pad[8 - sizeof(Desc *)]; };"}
        )
        holder = layout.homes[self.root / "holder.h"]
        self.assertIn("typedef struct Desc Desc;", layout.headers[holder].decode())

    def test_replayed_declarations_retain_header_typedefs_but_exclude_source_local_aliases(self):
        seed = declarations.published(
            (
                "typedef int (*Handler)(int);\n",
                "typedef float Local; extern Handler callback; int f(void) { return 0; }",
            ),
            {},
            Path("f.c"),
        )
        self.assertIn("Handler", seed["shared_typedefs"])
        self.assertNotIn("Local", seed["shared_typedefs"])
        imported = (
            '# 1 "shared/types/callback.h"\ntypedef int (*Imported)(int);\n'
            '# 1 "f.c"\ntypedef float Local; int f(void) { return 0; }'
        )
        seed = declarations.published(("", imported), {}, Path("f.c"))
        self.assertIn("Imported", seed["shared_typedefs"])
        self.assertNotIn("Local", seed["shared_typedefs"])

    def test_retained_callback_alias_reuses_existing_prerequisite_provider(self):
        seed = declarations.extract("typedef void (*Callback)(void); struct A { Callback cb; };", {})
        value = {
            "structs": {n: {**r, "state": "known", "generated": True} for n, r in seed["structs"].items()},
            "typedefs": {"Callback": "void (*)(void)"},
            "functions": {},
            "globals": {},
            "arrays": {},
        }
        outputs = {}

        def capture(project, content, *args, **kwargs):
            outputs.update(
                {p.relative_to(self.root).as_posix(): data.decode() for p, data in content.items() if p.suffix == ".h"}
            )
            raise ValueError("captured")

        with (
            patch.object(database, "validate_headers", side_effect=capture),
            self.assertRaisesRegex(ValueError, "captured"),
        ):
            database.publish(self.project, value, {}, policy=self.policy)
        text = "\n".join(outputs.values())
        self.assertEqual(text.count("*Callback)"), 1)
        self.assertFalse(any("consumer_alias_Callback" in name for name in outputs))


class StatementMemoTests(unittest.TestCase):
    def test_equal_text_splits_once_and_returns_equal_tuples(self) -> None:
        from unbake import cache
        from unbake.typemap import split

        text = "struct B_memo_probe { int a; };\nint B_memo_probe_x;\n"
        cache.forget(["typemap-statements"])
        with patch.object(split, "_split", wraps=split._split) as inner:
            first, second = split.statements(text), split.statements(text)
        self.assertEqual(inner.call_count, 1)
        self.assertEqual(first, second)
        self.assertIsInstance(first, tuple)
