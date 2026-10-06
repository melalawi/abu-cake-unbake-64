"""Type clusters are placed by the set of groups that use them, never one type per header."""

import unittest
from pathlib import Path
from typing import ClassVar
from unittest.mock import patch

from unbake.layout import headers
from unbake.layout.headers import Layout, co_usage, shared_name
from unbake.layout.map import Group, Map

ROOT = Path("/project/include")
G = {name: f"main/{name}.h" for name in ("ga", "gb", "gc", "gd")}
GROUPS = Map(32, tuple(Group(name, "main", "default", (name[1],)) for name in G))


def key(*names: str) -> frozenset[str]:
    return frozenset(G[name] for name in names)


def layout(types: dict[str, str], sources: dict[str, str]) -> Layout:
    contents = {ROOT / f".{name}.h": text for name, text in types.items()}
    return Layout(
        contents,
        contents,
        ROOT,
        ownership=GROUPS,
        sources={Path(f"/project/src/{member}.c"): text for member, text in sources.items()},
    )


def home(result: Layout, name: str) -> str:
    return result.homes[ROOT / f".{name}.h"].relative_to(ROOT).as_posix()


class CoUsageTests(unittest.TestCase):
    def test_small_sets_join_the_nearest_kept_set(self) -> None:
        for name, sizes, expected in (
            ("all kept", {key("ga", "gb"): 10, key("gc", "gd"): 10}, {}),
            (
                "highest overlap wins",
                {key("ga", "gb", "gc"): 10, key("gc", "gd"): 10, key("ga", "gb"): 1},
                {key("ga", "gb"): key("ga", "gb", "gc")},
            ),
            (
                "overlap and size tie takes the first name",
                {key("ga", "gb", "gc"): 10, key("gb", "gc", "gd"): 10, key("gb", "gc"): 1},
                {key("gb", "gc"): key("ga", "gb", "gc")},
            ),
            (
                "nothing at the floor keeps the largest",
                {key("ga", "gb"): 3, key("gc", "gd"): 2},
                {key("gc", "gd"): key("ga", "gb")},
            ),
        ):
            with self.subTest(name):
                target = co_usage(sizes, 10)
                self.assertEqual({k: v for k, v in target.items() if k != v}, expected)
                self.assertEqual(set(target), set(sizes))


class PlacementTests(unittest.TestCase):
    TYPES: ClassVar[dict[str, str]] = {
        "Own": "typedef struct Own { int a; } Own;",
        "Pair": "typedef struct Pair { Inner in; } Pair;",
        "Inner": "typedef struct Inner { int b; } Inner;",
        "Wide": "typedef struct Wide { int c; } Wide;",
        "Idle": "typedef struct Idle { Inner in; } Idle;",
    }
    SOURCES: ClassVar[dict[str, str]] = {
        "a": "void a(void) { Own o; Pair p; Wide w; }",
        "b": "void b(void) { Pair p; Wide w; }",
        "c": "void c(void) { Inner i; Wide w; }",
        "d": "void d(void) { Wide w; }",
    }

    def test_homes_follow_using_groups(self) -> None:
        with patch.object(headers, "SHARED_MIN_BYTES", 1):
            result = layout(self.TYPES, self.SOURCES)
        for name, expected in (
            ("Own", G["ga"]),
            ("Pair", shared_name(key("ga", "gb"))),
            # A dependency is used by every user of its dependents.
            ("Inner", shared_name(key("ga", "gb", "gc"))),
            ("Wide", shared_name(key("ga", "gb", "gc", "gd"))),
            ("Idle", "common/unused.h"),
        ):
            with self.subTest(name):
                self.assertEqual(home(result, name), expected)
        names = {path.relative_to(ROOT).as_posix() for path in result.headers}
        self.assertNotIn("common/types.h", names)
        self.assertNotIn("main/types.h", names)
        # A group header holds its private types; it does not include the shared homes its sources use.
        self.assertIn(b"struct Own", result.headers[ROOT / G["ga"]])
        self.assertNotIn(b"#include", result.headers[ROOT / G["ga"]])
        pair = result.headers[ROOT / shared_name(key("ga", "gb"))].decode()
        self.assertIn(f'#include "{shared_name(key("ga", "gb", "gc"))}"', pair)

    def test_small_sets_share_one_substantial_header(self) -> None:
        result = layout(self.TYPES, self.SOURCES)
        shared = {home(result, name) for name in ("Pair", "Inner", "Wide")}
        self.assertEqual(len(shared), 1)
        self.assertTrue(headers.shared_header(shared.pop()))

    def test_bytes_are_deterministic(self) -> None:
        first = layout(self.TYPES, self.SOURCES)
        second = layout(dict(reversed(self.TYPES.items())), dict(reversed(self.SOURCES.items())))
        self.assertEqual(first.headers, second.headers)
        self.assertEqual(first.index, second.index)


class DeclarationHomeTests(unittest.TestCase):
    def test_function_declarations_stay_in_their_module(self) -> None:
        types = {"Inner": "typedef struct Inner { int b; } Inner;", "decl": "extern int b(Inner *p);"}
        stale = {ROOT / ".decl.h": {ROOT / "main/old.h"}, ROOT / ".Inner.h": {ROOT / G["gc"]}}
        contents = {ROOT / f".{name}.h": text for name, text in types.items()}
        sources = {"a": "void a(void) { b(0); }", "c": "void c(void) { b(0); }"}
        result = Layout(
            contents,
            contents,
            ROOT,
            ownership=GROUPS,
            sources={Path(f"/project/src/{member}.c"): text for member, text in sources.items()},
            fixed_homes=stale,
        )
        # Used by ga and gc, but declared by gb: the declaration lives in gb's module header, never a shared one.
        self.assertEqual(home(result, "decl"), G["gb"])
        # A type's recorded home that is still a module header keeps providing it; a vanished one is not recreated.
        self.assertIn(f'#include "{home(result, "Inner")}"', result.headers[ROOT / G["gc"]].decode())
        self.assertNotIn(ROOT / "main/old.h", result.headers)


class OneHomeTests(unittest.TestCase):
    def test_a_layout_two_units_share_has_one_home_their_headers_include(self) -> None:
        with patch.object(headers, "SHARED_MIN_BYTES", 1):
            result = layout(
                {"Shared": "typedef struct Shared { int a; } Shared;"},
                {"a": "void a(void) { Shared s; }", "b": "void b(void) { Shared s; }"},
            )
        home_header = home(result, "Shared")
        self.assertEqual(home_header, shared_name(key("ga", "gb")))
        defining = [p for p, data in result.headers.items() if b"struct Shared {" in data]
        self.assertEqual([p.relative_to(ROOT).as_posix() for p in defining], [home_header])
        self.assertEqual(result.index["type_headers"]["Shared"], [home_header])

    def test_one_name_from_two_components_is_refused_naming_its_home(self) -> None:
        for label, types, sources, expected in [
            (
                "same tag, different layouts, both modules",
                {"A": "struct Unit { int a; };", "B": "struct Unit { char b[8]; };"},
                {"a": "void a(void) { struct Unit u; }", "b": "void b(void) { struct Unit u; }"},
                r"struct Unit would be defined in common/types_[0-9a-f]{12}\.h twice",
            ),
            (
                "same typedef twice in one module",
                {"A": "typedef int Count;", "B": "typedef int Count;"},
                {"a": "void a(void) { Count c; }"},
                "typedef Count would be defined in main/ga.h twice",
            ),
        ]:
            with self.subTest(label), self.assertRaisesRegex(Exception, "headers.type_home: " + expected):
                layout(types, sources)


class AuthoredIncludeOrderTests(unittest.TestCase):
    def test_authored_scalar_provider_precedes_its_view_in_a_group(self) -> None:
        contents = {
            ROOT / "types.h": "typedef short s16;",
            ROOT / "common/view.h": "struct View { s16 value; };",
        }
        with patch.object(headers, "guarded", side_effect=lambda path, text: text.encode()):
            result = Layout(
                contents,
                contents,
                ROOT,
                ownership=GROUPS,
                sources={Path("/project/src/a.c"): "s16 a(void) { struct View v; return v.value; }"},
                authored=set(contents),
            )
        group = result.headers[ROOT / G["ga"]].decode()
        self.assertLess(group.index('#include "../types.h"'), group.index('#include "common/view.h"'))
