"""Shared type emission and consumer dependency contracts use mocked tools."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.decomp.support import fixture
from tests.typemap.split_support import expanded
from unbake.project.config import Held
from unbake.typemap import database, declarations, header_names, storage
from unbake.typemap.split import Layout, statements


class SplitTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(dir=os.environ["TMPDIR"])
        self.addCleanup(directory.cleanup)
        self.project, self.policy, _ = fixture(Path(directory.name).resolve(), case=self)
        self.root = self.project.include[0]

    def layout(self, texts):
        components = {self.root / name: text for name, text in texts.items()}
        return Layout(components, components, self.root)

    def test_transitive_closure_excludes_unused_layout_and_orders_value_dependencies(self):
        layout = self.layout(
            {
                "word.h": "typedef int Word;",
                "a.h": "struct A { Word value; };",
                "b.h": "struct B { struct A value; };",
                "unused.h": "struct Unused { double huge[100]; };",
            }
        )
        consumer = self.root / "shared/decls/f.h"
        outputs = {p.relative_to(self.root).as_posix(): data.decode() for p, data in layout.headers.items()}
        outputs["shared/decls/f.h"] = layout.consumer(consumer, "extern struct B *f(void);").decode()
        text = expanded(outputs, "shared/decls/f.h")
        self.assertLess(text.index("typedef int Word"), text.index("struct A {"))
        self.assertLess(text.index("struct A {"), text.index("struct B {"))
        self.assertNotIn("struct Unused", text)
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
        p = self.root / "consumer.h"
        outputs["consumer.h"] = layout.consumer(p, "extern A *head;").decode()
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

    def test_guard_names_and_paths_are_stable_across_runs_and_unique(self):
        texts = {"a.h": "struct A { int x; };", "b.h": "struct B { int x; };"}
        first, second = self.layout(texts), self.layout(dict(reversed(list(texts.items()))))
        self.assertEqual(first.headers, second.headers)
        guards = [data.decode().splitlines()[0] for data in first.headers.values()]
        self.assertEqual(len(guards), len(set(guards)))
        for path, data in first.headers.items():
            self.assertTrue(storage.generated(self.project, path))
            self.assertTrue(data.endswith(b"#endif\n"))

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

    def test_collision_reservation_follows_split_and_declaration_headers(self):
        (self.root / "shared/types").mkdir(parents=True)
        (self.root / "shared/decls").mkdir(parents=True)
        (self.root / "shared/types/a.h").write_text("struct A { int x; };")
        (self.root / "shared/decls/f.h").write_text('#include "shared/types/a.h"\n')
        source = self.project.src / "published.c"
        source.write_text('#include "shared/decls/f.h"\ntypedef int Local; struct Tag;')
        self.assertEqual(
            header_names.source_names(self.project, self.root / "shared/typemap.h", self.policy), {"Local", "Tag"}
        )

    def test_fresh_mocked_project_solve_publish_and_consumer_compile_have_no_umbrella_or_migration_macro(self):
        from unbake.project import makefile
        from unbake.typemap import map_program, solver

        # fixture initializes configuration and runs setup with mocked tool
        # boundaries. Solve and publish are real; the compiler is mocked.
        bodies = {
            "alpha": "Owner *alpha(Owner *p) { return p; }",
            "beta": "typedef struct Owner Owner; int beta(Owner *p) { return p->value; }",
            "gamma": "int gamma(Handler802A2B50 callback, Owner *p) { return callback(p, 0, 0, 0, 0); }",
        }
        for name, body in bodies.items():
            (self.project.src / (name + ".c")).write_text(f'#include "shared/decls/{name}.h"\n' + body)
        seed = declarations.extract(
            "typedef struct Owner { int value; } Owner;"
            "typedef int (*Handler802A2B50)(void *, int, int, int, int);"
            "Owner *alpha(Owner *p); int beta(Owner *p); int gamma(Handler802A2B50 callback, Owner *p);",
            {"kind": "declared"},
        )
        facts = map_program(self.project)
        empty = {**facts, "functions": {}, "globals": {}}
        with (
            patch.object(solver, "refresh_map", return_value=empty),
            patch("unbake.typemap.abi_facts.refine", return_value=empty),
            patch.object(declarations, "collect", return_value=[seed]),
        ):
            result = solver.solve(self.project, self.policy)
        self.assertIn("Handler802A2B50", result["typedefs"])
        self.assertFalse((self.root / "shared/typemap.h").exists())
        self.assertFalse((self.root / "shared/prototypes.h").exists())
        self.assertFalse((self.root / "shared/consumers").exists())
        outputs = {p.relative_to(self.root).as_posix(): p.read_text() for p in self.root.rglob("*.h")}
        for name in bodies:
            source = self.project.src / (name + ".c")
            flags = makefile.flags(self.project, "us", source)
            self.assertFalse(any("UNBAKE_CONSUMER" in flag for flag in flags))
            self.assertNotIn("typemap.h", source.read_text())
            self.assertNotIn("UNBAKE_CONSUMER", source.read_text())
            header = expanded(outputs, f"shared/decls/{name}.h")
            self.assertNotIn("typemap.h", header)
            if name == "beta":
                self.assertNotIn("typedef struct Owner Owner;", header)
            else:
                self.assertIn("typedef struct Owner Owner;", header)
            parsed = declarations.extract(declarations.clean(header) + "\n" + bodies[name], {}, definitions=True)
            self.assertIn(name, parsed["functions"])
        self.assertIn("Handler802A2B50", expanded(outputs, "shared/decls/gamma.h"))

    def test_reserved_alias_is_received_only_by_consumer_without_local_declaration(self):
        from unbake.typemap.split import consumer_macro

        shared = self.root / "shared/common.h"
        shared.parent.mkdir(exist_ok=True)
        shared.write_text('#ifndef COMMON_H\n#define COMMON_H\n#include "shared/typemap.h"\n#endif\n')
        owner = self.project.src / "owner.c"
        user = self.project.src / "user.c"
        owner.write_text(
            '#include "shared/common.h"\ntypedef struct Owner Owner; int local(Owner *p) { return p->value; }'
        )
        user.write_text(
            '#include "shared/common.h"\nvoid f(Owner *owner, Rider *rider) { owner->value = rider->value; }'
        )
        seed = declarations.extract(
            "typedef struct Owner { int value; } Owner; typedef struct Rider { int value; } Rider;", {}
        )
        value = {
            "structs": {n: {**r, "state": "known", "generated": True} for n, r in seed["structs"].items()},
            "functions": {},
            "globals": {},
            "arrays": {},
        }
        outputs = {}

        def capture(project, content, *args, **kwargs):
            outputs.update({p.relative_to(self.root).as_posix(): data.decode() for p, data in content.items()})
            raise ValueError("captured")

        with (
            patch.object(database, "validate_headers", side_effect=capture),
            self.assertRaisesRegex(ValueError, "captured"),
        ):
            database.publish(self.project, value, {}, policy=self.policy)
        local = expanded(outputs, "shared/consumers/owner.h")
        remote = expanded(outputs, "shared/consumers/user.h")
        self.assertNotIn("typedef struct Owner Owner;", local)
        self.assertIn("struct Owner {", local)
        self.assertIn("typedef struct Owner Owner;", remote)
        self.assertIn("typedef struct Rider Rider;", remote)
        self.assertIn(consumer_macro("user"), outputs["shared/common.h"])
        self.assertLess(
            outputs["shared/common.h"].index(consumer_macro("user")),
            outputs["shared/common.h"].index("#ifndef COMMON_H"),
        )
        self.assertEqual(
            user.read_text(),
            '#include "shared/common.h"\nvoid f(Owner *owner, Rider *rider) { owner->value = rider->value; }',
        )

    def test_consumer_ownership_follows_only_authored_include_closure(self):
        from unbake.typemap.regeneration import Session

        rows = (
            ("direct", '#include "local.h"\n', "local.h", True),
            ("nested", '#include "nested.h"\n', "local.h", True),
            ("generated", '#include "shared/types/local.h"\n', "shared/types/local.h", False),
            ("bridge", '#include "shared/types/bridge.h"\n', "local.h", True),
            ("unrelated", "", "local.h", False),
        )
        for label, include, home, expected in rows:
            with self.subTest(label=label), tempfile.TemporaryDirectory(dir=os.environ["TMPDIR"]) as directory:
                project, policy, _ = fixture(Path(directory).resolve(), case=self)
                root = project.include[0]
                (root / "shared").mkdir(exist_ok=True)
                (root / "shared/typemap.h").write_text("")
                path = root / home
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(
                    "typedef int Handler; struct LocalTag { int value; }; int callback(void); typedef int M2C_UNK;"
                )
                (root / "nested.h").write_text('#include "local.h"\n')
                (root / "shared/types").mkdir(exist_ok=True)
                (root / "shared/types/bridge.h").write_text('#include "local.h"\n')
                source = project.src / "user.c"
                source.write_text(
                    include + '#include "shared/typemap.h"\nvoid user(Handler h, struct LocalTag *tag) { callback(); }'
                )
                other = project.src / "other.c"
                other.write_text('#include "shared/typemap.h"\nvoid other(Handler h) {}')
                session = Session(project, policy)
                for _cached in (False, True):
                    consumers = {}
                    self.assertEqual(session.source_names(consumers), set())
                    self.assertEqual(
                        consumers[source],
                        {"Handler", "LocalTag", "callback", "M2C_UNK"} if expected else set(),
                    )
                    self.assertEqual(consumers[other], set())
                    self.assertEqual(session.consumer_tags[source], {"LocalTag"} if expected else set())
                    layout = Layout(
                        {root / "alias.h": "typedef int Handler;", root / "tag.h": "struct LocalTag { int value; };"},
                        {root / "alias.h": "typedef int Handler;", root / "tag.h": "struct LocalTag { int value; };"},
                        root,
                    )
                    required = layout.required(
                        session.sources[source], blocked=consumers[source], blocked_tags=session.consumer_tags[source]
                    )
                    self.assertEqual(required, set() if expected else set(layout.headers))
                    self.assertEqual(
                        layout.required(session.sources[other], blocked=consumers[other]),
                        {layout.homes[root / "alias.h"]},
                    )

    def test_authored_scalar_compatibility_is_excluded_per_consumer(self):
        for nested in (False, True):
            with self.subTest(nested=nested), tempfile.TemporaryDirectory(dir=os.environ["TMPDIR"]) as directory:
                project, policy, _ = fixture(Path(directory).resolve(), case=self)
                root = project.include[0]
                (root / "shared").mkdir(exist_ok=True)
                (root / "shared/typemap.h").write_text("")
                (root / "scalar.h").write_text("typedef int M2C_UNK;")
                (root / "nested.h").write_text('#include "scalar.h"\n')
                name = "nested.h" if nested else "scalar.h"
                (project.src / "local.c").write_text(
                    f'#include "{name}"\n#include "shared/typemap.h"\nM2C_UNK local(void);'
                )
                (project.src / "remote.c").write_text('#include "shared/typemap.h"\nM2C_UNK remote(void);')
                value = {"structs": {}, "functions": {}, "globals": {}, "arrays": {}}
                outputs = {}

                def capture(project, content, *args, outputs=outputs, root=root, **kwargs):
                    outputs.update({p.relative_to(root).as_posix(): data.decode() for p, data in content.items()})
                    raise ValueError("captured")

                with (
                    patch.object(database, "validate_headers", side_effect=capture),
                    self.assertRaisesRegex(ValueError, "captured"),
                ):
                    database.publish(project, value, {}, policy=policy)
                self.assertNotIn("shared/consumers/local.h", outputs)
                self.assertIn("compat_M2C_UNK.h", outputs["shared/consumers/remote.h"])

    def test_callback_used_by_global_survives_solve_and_emission(self):
        from tests.typemap.test_solver import solve

        value = solve(
            {}, "typedef int (*Handler802A2B50)(void *, int, int, int, int); extern Handler802A2B50 callback;"
        )
        self.assertIn("Handler802A2B50", value["typedefs"])
        outputs = {}

        def capture(project, content, *args, **kwargs):
            outputs.update({p.relative_to(self.root).as_posix(): data.decode() for p, data in content.items()})
            raise ValueError("captured")

        with (
            patch.object(database, "validate_headers", side_effect=capture),
            self.assertRaisesRegex(ValueError, "captured"),
        ):
            database.publish(self.project, value, {}, policy=self.policy)
        home = next(name for name in outputs if "consumer_alias_Handler802A2B50" in name)
        text = expanded(outputs, home)
        self.assertIn("Handler802A2B50", text)
        declarations.extract(declarations.clean(text) + "\nextern Handler802A2B50 other;", {})

    def test_sizeof_typedef_dependencies_are_closed(self):
        layout = self.layout(
            {"desc.h": "typedef struct Desc Desc;", "holder.h": "struct Holder { char pad[8 - sizeof(Desc *)]; };"}
        )
        holder = layout.homes[self.root / "holder.h"]
        self.assertIn(layout.include(layout.homes[self.root / "desc.h"]), layout.headers[holder].decode())

    def test_replayed_declarations_retain_header_typedefs_but_exclude_source_local_aliases(self):
        batch = declarations._PublishedDeclarations()
        seed = batch.extract(
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
        seed = batch.extract(("", imported), {}, Path("f.c"))
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
            outputs.update({p.relative_to(self.root).as_posix(): data.decode() for p, data in content.items()})
            raise ValueError("captured")

        with (
            patch.object(database, "validate_headers", side_effect=capture),
            self.assertRaisesRegex(ValueError, "captured"),
        ):
            database.publish(self.project, value, {}, policy=self.policy)
        text = "\n".join(outputs.values())
        self.assertEqual(text.count("*Callback)"), 1)
        self.assertFalse(any("consumer_alias_Callback" in name for name in outputs))

    def test_legacy_missing_scalar_return_alias_is_supplied_only_to_its_consumer(self):
        from unbake.typemap.split import consumer_macro

        source = self.project.src / "alpha.c"
        source.write_text('#include "shared/typemap.h"\nM2C_UNK func_80262C88_de(); int alpha(void) { return 0; }')
        shared = self.root / "shared"
        shared.mkdir(exist_ok=True)
        (shared / "typemap.h").write_text("")
        value = {"structs": {}, "functions": {}, "globals": {}, "arrays": {}}
        outputs = {}

        def capture(project, content, *args, **kwargs):
            outputs.update({p.relative_to(self.root).as_posix(): data.decode() for p, data in content.items()})
            raise ValueError("captured")

        with (
            patch.object(database, "validate_headers", side_effect=capture),
            self.assertRaisesRegex(ValueError, "captured"),
        ):
            database.publish(self.project, value, {}, policy=self.policy)
        self.assertIn(consumer_macro("alpha"), outputs["shared/typemap.h"])
        self.assertIn("compat_M2C_UNK.h", outputs["shared/consumers/alpha.h"])
        header = expanded(outputs, "shared/consumers/alpha.h")
        self.assertIn("typedef s32 M2C_UNK;", header)
        declarations.extract(
            "typedef signed int s32;\n" + declarations.clean(header) + "\nM2C_UNK func_80262C88_de();", {}
        )
        self.assertFalse(any("M2C_UNK" in text for name, text in outputs.items() if name.startswith("shared/types/")))
        source.write_text('#include "shared/typemap.h"\ntypedef int M2C_UNK; M2C_UNK func_80262C88_de();')
        outputs.clear()
        with (
            patch.object(database, "validate_headers", side_effect=capture),
            self.assertRaisesRegex(ValueError, "captured"),
        ):
            database.publish(self.project, value, {}, policy=self.policy)
        self.assertNotIn("shared/consumers/compat_M2C_UNK.h", outputs)
