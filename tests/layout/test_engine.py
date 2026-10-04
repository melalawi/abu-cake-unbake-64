"""Pure ownership, rendering and publication contracts with mocked project inputs."""

import copy
import hashlib
import os
import tempfile
import tomllib
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from unbake.layout import apply, headers, index, map, redeclarations
from unbake.project.config import Held


class MapTests(unittest.TestCase):
    def setUp(self):
        self.members = {
            "first": map.Member("first", "span", 0x80001000, ("a", "b")),
            "second": map.Member("second", "span", 0x80001004, ("a",)),
            "third": map.Member("third", "other", 0x80002000, ("b",)),
        }
        self.value = {
            "schema": 1,
            "cap": 2,
            "group": [
                {
                    "name": "code",
                    "segment": "span",
                    "evidence": "default",
                    "members": ["first", "second"],
                    "only": {"second": ["a"]},
                    "split": ["second"],
                },
                {"name": "tail", "evidence": "proven", "members": ["third"]},
            ],
        }

    def test_explicit_member_edits_preserve_group_identity_and_cuts(self):
        with tempfile.TemporaryDirectory() as temporary:
            project = SimpleNamespace(root=Path(temporary), versions=("a", "b"))
            (project.root / "layout.toml").write_bytes(
                map.encoded(map.validate(self.value, project.versions, self.members))
            )
            original = (project.root / "layout.toml").read_bytes()
            mirror = project.root / "layout-before.toml"
            os.link(project.root / "layout.toml", mirror)
            renamed = {**self.members, "replacement": map.Member("replacement", "span", 0x80001004, ("a",))}
            del renamed["second"]
            with patch.object(map, "catalog", return_value=renamed):
                map.edit_members(project, {"second": ("replacement",)})
                result = map.load(project)
            self.assertEqual(mirror.read_bytes(), original)
            self.assertEqual(result.groups[0].name, "code")
            self.assertEqual(result.groups[0].members, ("first", "replacement"))
            self.assertEqual(result.groups[0].split, ("replacement",))
            self.assertEqual(result.groups[0].only, {"replacement": ("a",)})
            self.assertEqual(result.groups[1].only, {})

    def test_every_refusal_is_named_and_held_in_layout(self):
        cases = [
            ("map.extra", lambda v: v.update(extra=1)),
            ("schema", lambda v: v.update(schema=2)),
            ("schema", lambda v: v.update(schema=True)),
            ("cap", lambda v: v.update(cap=0)),
            ("cap", lambda v: v.update(cap=True)),
            ("cap", lambda v: v.pop("cap")),
            ("group", lambda v: v.update(group=[])),
            ("group", lambda v: v.update(group="bad")),
            ("group", lambda v: v["group"].__setitem__(0, "bad")),
            ("group.extra", lambda v: v["group"][0].update(extra=1)),
            ("group.name", lambda v: v["group"][0].update(name="../escape")),
            ("group.types", lambda v: v["group"][0].update(name="types")),
            ("group.code.members", lambda v: v["group"][0].update(members=[])),
            ("group.code.members", lambda v: v["group"][0].update(members=[1])),
            ("member.first", lambda v: v["group"][0].update(members=["first", "first"])),
            ("member.absent", lambda v: v["group"][0].update(members=["absent"])),
            ("member.first", lambda v: v["group"][1].update(members=["first"])),
            ("group.code.members", lambda v: v["group"][0].update(members=["second", "first"])),
            ("group.code.segment", lambda v: v["group"][0].update(segment="other")),
            ("group.code.segment", lambda v: v["group"][0].update(members=["first", "third"])),
            ("group.code.evidence", lambda v: v["group"][0].update(evidence="guessed")),
            ("group.code.only", lambda v: v["group"][0].update(only=[])),
            ("only.third", lambda v: v["group"][0].update(only={"third": ["a"]})),
            ("only.second", lambda v: v["group"][0].update(only={"second": []})),
            ("only.second.missing", lambda v: v["group"][0].update(only={"second": ["missing"]})),
            ("only.second", lambda v: v["group"][0].update(only={"second": ["a", "a"]})),
            ("group.code.split", lambda v: v["group"][0].update(split=["third"])),
            ("group.code.split", lambda v: v["group"][0].update(split=["second", "second"])),
            ("member.third", lambda v: v["group"].pop()),
        ]
        for key, mutate in cases:
            with self.subTest(key=key):
                value = copy.deepcopy(self.value)
                mutate(value)
                with self.assertRaises(Held) as caught:
                    map.validate(value, ("a", "b"), self.members)
                self.assertEqual(caught.exception.phase, "layout")
                self.assertIn("layout." + key, caught.exception.reason)

    def test_map_round_trip_and_inferred_segment(self):
        value = map.validate(self.value, ("a", "b"), self.members)
        self.assertEqual(value.groups[1].segment, "other")
        self.assertEqual(map.validate(tomllib.loads(map.encoded(value).decode()), ("a", "b"), self.members), value)

    def test_catalog_anchors_addresses_in_names_from_and_reads_quoted_segments(self):
        project = SimpleNamespace(versions=("a", "b"), names_from="b", version=lambda v: SimpleNamespace(split=v))
        segment = SimpleNamespace(fields={"name": '"span"', "start": "0x1000"}, rows=[SimpleNamespace(path="first")])
        with (
            patch.object(map.split, "layout", return_value=(None, None, [segment])),
            patch.object(
                map.split,
                "functions",
                side_effect=lambda p, v: [SimpleNamespace(path="first", address={"a": 0x80000000, "b": 0x90000000}[v])],
            ),
        ):
            self.assertEqual(map.catalog(project), {"first": map.Member("first", "span", 0x90000000, ("b", "a"))})

    def test_default_boundaries_cap_address_names_and_version_marks(self):
        for cap, lengths in ((1, [1, 1, 1]), (2, [2, 1]), (10, [2, 1])):
            with self.subTest(cap=cap):
                result = map.default(cap, dict(reversed(list(self.members.items()))), ("a", "b"))
                self.assertEqual([len(g.members) for g in result.groups], lengths)
                self.assertEqual(result.groups[0].name, "code_80001000")
                self.assertEqual(result.groups[-1].name, "code_80002000")
                marks = {k: v for g in result.groups for k, v in g.only.items()}
                self.assertEqual(marks, {"second": ("a",), "third": ("b",)})
        with self.assertRaisesRegex(Held, "layout.cap"):
            map.default(0, self.members, ("a", "b"))

    def test_setup_missing_cap_refuses_without_tool_default(self):
        with tempfile.TemporaryDirectory(dir=os.environ["TMPDIR"]) as folder:
            root = Path(folder)
            (root / "config.toml").write_text("[project]\n")
            with self.assertRaisesRegex(Held, "project.layout_cap"):
                map.ensure(SimpleNamespace(root=root))
            (root / "config.toml").write_text("[project]\nlayout_cap = 2\n")
            project = SimpleNamespace(root=root, versions=("a", "b"))
            with patch.object(map, "catalog", return_value=self.members):
                map.ensure(project)
                before = (root / "layout.toml").stat().st_mtime_ns
                map.ensure(project)
                self.assertEqual(before, (root / "layout.toml").stat().st_mtime_ns)


class HeaderTests(unittest.TestCase):
    def setUp(self):
        self.root = Path("/project/include")
        self.ownership = map.Map(
            2,
            (
                map.Group("one", "span", "default", ("first",)),
                map.Group("two", "span", "default", ("second",)),
                map.Group("three", "other", "default", ("third",)),
            ),
        )

    def render(self, contents, sources, **kwargs):
        contents = {self.root / name: text for name, text in contents.items()}
        sources = {Path("/project/src") / (name + ".c"): text for name, text in sources.items()}
        return headers.Layout(contents, contents, self.root, ownership=self.ownership, sources=sources, **kwargs)

    def test_published_global_stays_visible_at_its_original_data_home(self):
        published = self.root / ".published_cell.h"
        result = self.render(
            {".published_cell.h": "extern void *cell;"},
            {
                "first": '#include "span/data.h"\nvoid first(void *p) { cell = p; }',
                "second": "extern void *cell[4]; void second(void) { cell[0] = 0; }",
            },
            fixed_homes={published: {self.root / "span/data.h"}},
        )
        self.assertIn(b"extern void *cell;", result.headers[self.root / "span/data.h"])
        for home in ("span/one.h", "span/two.h", "span/types.h", "common/types.h"):
            self.assertNotIn(b"extern void *cell;", result.headers[self.root / home])
        self.assertEqual(result.index["symbols"]["cell"], "span/data.h")

    def test_published_type_can_widen_without_losing_original_include_homes(self):
        published = self.root / ".published_record.h"
        for homes in ({self.root / "span/one.h"}, {self.root / "span/one.h", self.root / "span/data.h"}):
            result = self.render(
                {".published_record.h": "struct Record { int word; };"},
                {"first": '#include "span/one.h"\nstruct Record *first(void);'},
                fixed_homes={published: homes},
                declarations_by_name={"cell": "extern struct Record cell;"},
                symbol_segments={"cell": "span"},
            )
            self.assertIn(b"struct Record { int word; };", result.headers[self.root / "span/types.h"])
            for home in homes:
                self.assertIn(b'#include "span/types.h"', result.headers[home])

    def test_source_imports_complete_tag_home_as_well_as_alias_home(self):
        result = self.render(
            {"alias.h": "typedef struct Record Record;", "body.h": "struct Record {int value;};"},
            {"first": "Record *first;", "second": "struct Record *second;"},
        )
        homes = result.index["type_headers"]["Record"]
        self.assertEqual(
            set(homes),
            {
                result.homes[self.root / "alias.h"].relative_to(self.root).as_posix(),
                result.homes[self.root / "body.h"].relative_to(self.root).as_posix(),
            },
        )
        rewritten = apply.rewrite("Record *first;", "first", self.ownership, result.index, previous=set())
        for home in homes:
            self.assertIn('#include "' + home + '"', rewritten)

    def test_source_type_dependency_is_retained_without_a_shared_prototype(self):
        authored = self.root / "types.h"
        result = self.render(
            {"types.h": "typedef int Scalar;"},
            {"first": "Scalar first(void) { return 1; }"},
            authored={authored},
        )
        self.assertIn(b'#include "../types.h"', result.headers[self.root / "span/one.h"])

    def test_builtin_aliases_compare_equal_inside_derived_declarators(self):
        mapping = redeclarations.aliases(
            ["typedef signed int s32; typedef signed short s16; typedef signed long long s64;"]
        )
        for left, right in (
            ("extern s32 data[];", "extern int data[];"),
            ("extern s16 *data;", "extern short *data;"),
            ("extern s64 data[];", "extern long long data[];"),
            ("extern signed int data[];", "extern int data[];"),
            ("extern s32 func(s32 *p);", "extern int func(int *p);"),
        ):
            with self.subTest(left=left):
                self.assertTrue(redeclarations.equivalent(left, right, mapping))

    def test_authored_root_include_cannot_resolve_to_segment_types(self):
        authored = self.root / "types.h"
        result = self.render(
            {"types.h": "typedef int Scalar;", "record.h": "typedef Scalar Record;"},
            {"first": "Record value;"},
            authored={authored},
        )
        self.assertIn(b'#include "../types.h"', result.headers[self.root / "span/one.h"])
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "span").mkdir()
            (root / "span/types.h").write_text("wrong sibling")
            output = {root / "span/one.h": b'#include "../types.h"', root / "types.h": b"pending authored root"}
            self.assertIn("pending authored root", apply.imported('#include "span/one.h"', root, output))
            self.assertNotIn("wrong sibling", apply.imported('#include "span/one.h"', root, output))

    def test_group_segment_common_and_transitive_users(self):
        cases = (
            ({"first": "Private p;"}, "span/one.h"),
            ({"first": "Private p;", "second": "Private q;"}, "span/types.h"),
            ({"first": "Private p;", "third": "Private q;"}, "common/types.h"),
        )
        for sources, expected in cases:
            with self.subTest(expected=expected):
                result = self.render({"private.h": "typedef Base Private;", "base.h": "typedef int Base;"}, sources)
                self.assertEqual(result.homes[self.root / "private.h"], self.root / expected)
                self.assertEqual(result.homes[self.root / "base.h"], self.root / expected)
                self.assertEqual(result.index["symbols"]["Private"], expected)

    def test_pointer_cycle_hoists_entire_cluster_and_keeps_forward_tags(self):
        result = self.render(
            {"a.h": "struct A { struct B *b; };", "b.h": "struct B { struct A *a; };"},
            {"first": "struct A a;", "third": "struct B b;"},
        )
        self.assertEqual(len(result.groups), 1)
        self.assertEqual(set(result.homes.values()), {self.root / "common/types.h"})
        body = result.headers[self.root / "common/types.h"].decode()
        self.assertLess(body.index("struct B;"), body.index("struct A {"))

    def test_value_cycle_is_held(self):
        with self.assertRaises(Held):
            self.render(
                {"a.h": "struct A { struct B b; };", "b.h": "struct B { struct A a; };"}, {"first": "struct A a;"}
            )

    def test_dependency_includes_go_up_and_sibling_headers_are_source_only(self):
        result = self.render(
            {"a.h": "typedef int A;", "b.h": "typedef struct B { A value; } B;"},
            {"first": "B b;", "third": "A a;"},
            declarations_by_name={"first": "extern B first(void);", "second": "extern int second(void);"},
        )
        body = result.headers[self.root / "span/one.h"].decode()
        self.assertIn('#include "common/types.h"', body)
        self.assertNotIn('#include "span/two.h"', body)
        self.assertIn("extern B first", body)

    def test_data_without_c_owner_uses_segment_data_header(self):
        result = self.render(
            {"type.h": "typedef int Value;"},
            {"first": "Value v;"},
            declarations_by_name={"global": "extern Value global;"},
            symbol_segments={"global": "span"},
        )
        self.assertEqual(result.index["symbols"]["global"], "span/data.h")
        self.assertEqual(result.homes[self.root / "type.h"], self.root / "span/types.h")
        self.assertIn(b'#include "span/types.h"', result.headers[self.root / "span/data.h"])

    def test_header_cycle_and_downward_edge_are_held(self):
        first, second = self.root / "span/one.h", self.root / "span/two.h"
        for edges, key in (
            ({first: {second}, second: {first}}, "layout.cycle"),
            ({first: {second}}, "layout.includes"),
        ):
            with self.subTest(key=key), self.assertRaisesRegex(Held, key):
                headers.validate_edges(self.root, edges, set())

    def test_permuted_component_input_is_byte_stable(self):
        a = {"a.h": "typedef int A;", "b.h": "typedef int B;"}
        self.assertEqual(
            self.render(a, {"first": "A a; B b;"}).headers,
            self.render(dict(reversed(list(a.items()))), {"first": "A a; B b;"}).headers,
        )


class ApplyTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(dir=os.environ["TMPDIR"])
        self.addCleanup(folder.cleanup)
        root = Path(folder.name)
        include, src = root / "include", root / "src"
        include.mkdir()
        src.mkdir()
        self.project = SimpleNamespace(root=root, include=(include,), src=src, build=root / "build", versions=())
        self.ownership = map.Map(
            2, (map.Group("one", "span", "default", ("first",)), map.Group("two", "span", "default", ("second",)))
        )
        self.lookup = {
            "schema": 1,
            "symbols": {"first": "span/one.h", "second": "span/two.h"},
            "clusters": {},
            "headers": {"span/one.h": "a" * 64, "span/two.h": "b" * 64},
        }

    def test_include_rewrite_preserves_body_and_is_idempotent(self):
        body = "int first(void) {return second();}\n"
        original = '#include "sdk.h"\n#include "old.h"\n#include "obsolete.h"\n' + body
        result = apply.rewrite(original, "first", self.ownership, self.lookup, previous={"old.h", "obsolete.h"})
        self.assertEqual(result, '#include "sdk.h"\n#include "span/one.h"\n#include "span/two.h"\n' + body)
        self.assertEqual(apply.rewrite(result, "first", self.ownership, self.lookup, previous=set()), result)
        with self.assertRaisesRegex(Held, "layout.member.missing"):
            apply.rewrite(body, "missing", self.ownership, self.lookup, previous=set())

    def test_index_round_trip_and_changed_bytes_only(self):
        outputs = {
            self.project.include[0] / "span/one.h": b"extern int first(void);\n",
            self.project.include[0] / "span/two.h": b"extern int second(void);\n",
        }
        value = copy.deepcopy(self.lookup)
        value["headers"] = {
            p.relative_to(self.project.include[0]).as_posix(): hashlib.sha256(data).hexdigest()
            for p, data in outputs.items()
        }
        value["clusters"] = {"virtual.h": "span/one.h"}
        outputs[index.path(self.project)] = index.encoded(value)
        self.assertEqual(apply.install(self.project, outputs, dry_run=True), 3)
        self.assertFalse(index.path(self.project).exists())
        self.assertEqual(apply.install(self.project, outputs), 3)
        self.assertEqual(index.load(self.project), value)
        stamps = {p: p.stat().st_mtime_ns for p in outputs}
        self.assertEqual(apply.install(self.project, outputs), 0)
        self.assertEqual(stamps, {p: p.stat().st_mtime_ns for p in outputs})

    def test_full_apply_second_run_writes_zero_files(self):
        (self.project.src / "first.c").write_text("int first(void) {return 1;}\n")
        output = self.project.include[0] / "span/one.h"
        data = b"extern int first(void);\n"
        lookup = {
            "schema": 1,
            "symbols": {"first": "span/one.h"},
            "clusters": {},
            "headers": {"span/one.h": hashlib.sha256(data).hexdigest()},
        }
        outputs = {output: data, index.path(self.project): index.encoded(lookup)}
        with (
            patch("unbake.typemap.database.load", return_value={}),
            patch("unbake.typemap.database._render", side_effect=lambda *a: dict(outputs)),
            patch("unbake.typemap.regeneration.Session") as session,
            patch.object(map, "load", return_value=self.ownership),
        ):
            session.side_effect = lambda *a: SimpleNamespace(
                ownership=self.ownership, sources={p: p.read_text() for p in self.project.src.glob("*.c")}
            )
            self.assertEqual(apply.run(self.project), 3)
            self.assertEqual(apply.run(self.project), 0)

    def test_stale_removal_is_index_only_and_dry_run_preserves_everything(self):
        old = self.project.include[0] / "old.h"
        authored = self.project.include[0] / "authored.h"
        old.write_bytes(b"old")
        authored.write_bytes(b"authored")
        index.path(self.project).parent.mkdir(parents=True)
        value = {"schema": 1, "symbols": {}, "clusters": {}, "headers": {"old.h": "a" * 64}}
        index.path(self.project).write_bytes(index.encoded(value))
        outputs = {index.path(self.project): index.encoded({"schema": 1, "symbols": {}, "clusters": {}, "headers": {}})}
        self.assertEqual(apply.install(self.project, outputs, dry_run=True), 2)
        self.assertTrue(old.exists())
        apply.install(self.project, outputs)
        self.assertFalse(old.exists())
        self.assertEqual(authored.read_bytes(), b"authored")

    def test_index_refuses_escaping_paths_and_unlisted_homes(self):
        index.path(self.project).parent.mkdir(parents=True)
        for name in ("../escape.h", "/absolute.h", "not-header.txt"):
            with self.subTest(name=name):
                value = {"schema": 1, "symbols": {}, "clusters": {}, "headers": {name: "a" * 64}}
                index.path(self.project).write_bytes(index.encoded(value))
                with self.assertRaisesRegex(Held, "layout.index"):
                    index.load(self.project)
        value = {"schema": 1, "symbols": {"first": "missing.h"}, "clusters": {}, "headers": {}}
        index.path(self.project).write_bytes(index.encoded(value))
        with self.assertRaisesRegex(Held, "unlisted"):
            index.load(self.project)

    def test_redeclarations_strip_identical_and_hold_both_conflicting_texts(self):
        for local, shared in (
            ("typedef int Unknown;", "typedef  int\nUnknown ;"),
            ("extern int data[4];", "extern int data [ 4 ];"),
            ("typedef int (*Callback)(int);", "typedef int ( * Callback ) ( int );"),
        ):
            with self.subTest(local=local):
                body = "int first(void) {return 1;}\n"
                self.assertEqual(redeclarations.strip(local + "\n" + body, [shared]), "\n" + body)
        local, shared = "typedef int Unknown;", "typedef float Unknown;"
        with self.assertRaises(Held) as caught:
            redeclarations.strip(local, [shared])
        self.assertEqual(caught.exception.phase, "layout")
        self.assertIn(local, caught.exception.reason)
        self.assertIn(shared, caught.exception.reason)
        self.assertEqual(
            redeclarations.strip("int first(void) {typedef int Unknown; return 1;}", [shared]),
            "int first(void) {typedef int Unknown; return 1;}",
        )


class ExternalPrototypeTests(unittest.TestCase):
    def test_implicit_external_prototypes_follow_the_same_byte_rule(self):
        for declaration in ("void api(int x);", "extern void api(int x);"):
            with self.subTest(declaration=declaration):
                self.assertEqual(redeclarations.strip(declaration, [declaration]), "")
        local, shared = "void api(int x);", "void api(float x);"
        with self.assertRaises(Held) as caught:
            redeclarations.strip(local, [shared])
        self.assertIn(local, caught.exception.reason)
        self.assertIn(shared, caught.exception.reason)
        self.assertEqual(redeclarations.strip("static void api(int x);", [shared]), "static void api(int x);")
        self.assertEqual(redeclarations.strip("int (*callback)(int);", [shared]), "int (*callback)(int);")


class CanonicalRedeclarationTests(unittest.TestCase):
    def test_alias_chains_pointer_types_and_parameter_names(self):
        shared = "typedef float f32; typedef f32 Scalar; typedef Scalar *Ptr; extern f32 api(Ptr value);"
        local = "float api(float *argument);"
        self.assertEqual(redeclarations.strip(local, [shared]), "")
        self.assertEqual(redeclarations.strip("extern Scalar data;", [shared + " extern float data;"]), "")

    def test_named_aggregate_typedefs_keep_tag_identity(self):
        shared = "typedef struct Node { Unknown field; } Node, *NodePtr; extern struct Node *api(struct Node *);"
        self.assertEqual(redeclarations.strip("extern NodePtr api(Node *argument);", [shared]), "")
        with self.assertRaises(Held):
            redeclarations.strip("extern struct Other *api(struct Other *);", [shared])

    def test_qualifiers_and_array_extents_remain_conflicts(self):
        for local, shared in (
            ("extern const float data;", "extern float data;"),
            ("extern int data[3];", "extern int data[4];"),
            ("void api(float x);", "void api(double x);"),
        ):
            with self.subTest(local=local), self.assertRaises(Held):
                redeclarations.strip(local, [shared])


class SourceOwnedTagTests(unittest.TestCase):
    def test_typedef_equal_tag_body_is_stripped_with_field_names_preserved(self):
        text = "extern struct Entry { f32 value; int f32; } *data;"
        rewritten, names = redeclarations.privatize_tags(
            text, ["typedef float f32; struct Entry {float value; int f32;};"], "api"
        )
        self.assertEqual(rewritten, "extern struct Entry  *data;")
        self.assertEqual(names, {})

    def test_equal_inline_extern_aggregate_is_removed_as_one_declaration(self):
        shared = "extern struct Entry {float value;} *data;"
        self.assertEqual(redeclarations.strip("extern struct Entry {float value;} *data;", [shared]), "")

    def test_struct_and_union_tags_keep_distinct_identities(self):
        rewritten, names = redeclarations.privatize_tags(
            "struct Entry { int value; };", ["union Entry { int value; };"], "api"
        )
        self.assertEqual(names, {"Entry": "Entry_api"})
        self.assertIn("struct Entry_api", rewritten)

    def test_private_tag_rename_preserves_values_comments_and_ordinary_identifiers(self):
        text = (
            "extern struct Entry { int value; } *data;\n"
            'const char *label = "struct Entry"; /* struct Entry */\n'
            "int api(void) { int Entry = 7; return ((struct Entry *)data)->value + Entry; }"
        )
        shared = "struct Entry { float unrelated; };"
        rewritten, names = redeclarations.privatize_tags(text, [shared], "api")
        self.assertEqual(names, {"Entry": "Entry_api"})
        self.assertIn("extern struct Entry_api { int value; } *data;", rewritten)
        self.assertIn("((struct Entry_api *)data)->value + Entry", rewritten)
        self.assertIn('"struct Entry"; /* struct Entry */', rewritten)
        self.assertIn("int Entry = 7", rewritten)
        again, names = redeclarations.privatize_tags(rewritten, [shared], "api")
        self.assertEqual(again, rewritten)
        self.assertEqual(names, {})
