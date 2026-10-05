"""Layered facts (a source part joined to header parts) equal the whole unit's facts, or refuse to stand in."""

import json
import unittest
from pathlib import Path

from unbake import cache, prefixes
from unbake.typemap import declarations, facts, layers

BOUNDARY = declarations.BOUNDARY + "\n"
TYPES = ("/r/include/types.h", "typedef int s32;\ntypedef float f32;\n")
# Line 1 of shapes.h includes types.h: preprocessed, it is an empty line (types.h already included) or its text.
SHAPES = ("/r/include/shapes.h", "\nstruct Shape { s32 kind; f32 size; };\ntypedef struct Shape Shape;\n")
DATA = "/r/include/data.h"
NESTED = {SHAPES[0]: (TYPES,), DATA: (TYPES,)}
SOURCE = Path("/r/src/alpha.c")


def marker(path: str, line: int = 1, flag: str = "") -> str:
    return f'# {line} "{path}"{" " + flag if flag else ""}\n'


def header_text(header: tuple[str, str], *nested: tuple[str, str]) -> str:
    """HEADER preprocessed alone (NESTED included by its first line), line-marked."""
    text = marker(header[0], 1, "1")
    body = header[1]
    for path, included in nested:
        text += marker(path, 1, "1") + included + marker(header[0], 2, "2")
        body = body.removeprefix("\n")
    return BOUNDARY + text + body


def unit(headers: list[tuple[str, str]], body: str) -> tuple[str, str]:
    """(line-marked unit, the same unit as cpp -P writes it) for a source including HEADERS then BODY."""
    marked = marker(str(SOURCE), 1, "1")
    plain = ""
    for line, (path, text) in enumerate(headers, 1):
        marked += marker(path, 1, "1") + text + marker(str(SOURCE), line + 1, "2")
        plain += text
    return BOUNDARY + marked + body, BOUNDARY + plain + body


def normal(seed: dict) -> object:
    seed = {k: dict(v) if isinstance(v, declarations.ProvenStructs) else v for k, v in seed.items()}
    if not seed.get("authored_structs"):
        seed.pop("authored_structs", None)
    return json.loads(json.dumps({k: {n: seed[k][n] for n in seed[k]} if k == "structs" else seed[k] for k in seed}))


def header_parts(headers: list[tuple[str, str]]) -> dict[str, dict]:
    return {
        layers.path(path): layers.header_part(header_text((path, text), *NESTED.get(path, ())), Path(path))
        for path, text in headers
    }


def layered(headers: list[tuple[str, str]], body: str, function: str) -> tuple[object, object] | None:
    marked, _ = unit(headers, body)
    provenance = {"kind": "published", "function": function, "version": "us", "source": "src/alpha.c"}
    parts = header_parts(headers)
    part = layers.source_part(marked, SOURCE, parts, provenance)
    if part is None:
        return None
    consumed, definition = layers.assemble(
        layers.Context(parts), part, SOURCE, body, lambda kind: {**provenance, "kind": kind}, frozenset()
    )
    return normal(consumed), normal(facts._owned(definition, function))


def whole(headers: list[tuple[str, str]], body: str, function: str) -> tuple[object, object]:
    _, plain = unit(headers, body)
    provenance = {"kind": "published", "function": function, "version": "us", "source": "src/alpha.c"}
    # facts._unit_seeds, with the source's text given rather than read.
    contract = declarations.published(plain, provenance, SOURCE, contracts=True, compact=True)
    consumed = declarations.consumed_contracts(contract, body)
    definition = declarations.published(plain, {**provenance, "kind": "proven"}, SOURCE, compact=True)
    return normal(consumed), normal(facts._owned(definition, function))


class LayeredFactsTests(unittest.TestCase):
    def setUp(self) -> None:
        cache.forget()
        prefixes.forget()

    def test_layered_equals_whole_unit(self) -> None:
        cases = [
            ("plain source", [TYPES, SHAPES], "Shape *alpha(s32 a) { return 0; }\n", "alpha"),
            (
                "consumed global and prototype",
                [TYPES, SHAPES],
                "extern Shape box;\nvoid use(Shape *);\nvoid alpha(void) { use(&box); }\n",
                "alpha",
            ),
            (
                "own struct embeds a header struct",
                [TYPES, SHAPES],
                "struct Pair { Shape left; Shape right; };\nvoid alpha(struct Pair *p) { }\n",
                "alpha",
            ),
            (
                "own typedef of a header struct",
                [TYPES, SHAPES],
                "typedef Shape Local;\nvoid alpha(Local *p) { }\n",
                "alpha",
            ),
            (
                "own anonymous struct typedef",
                [TYPES],
                "typedef struct { s32 x; } Point;\nvoid alpha(Point *p) { }\n",
                "alpha",
            ),
            ("second function of the source", [TYPES], "void alpha(void) { }\ns32 beta(f32 x) { return 0; }\n", "beta"),
        ]
        for label, headers, body, function in cases:
            with self.subTest(label):
                self.setUp()
                expected = whole(headers, body, function)
                self.setUp()
                self.assertEqual(layered(headers, body, function), expected)

    def test_a_source_respelling_a_header_typedef_keeps_whole_unit_facts(self) -> None:
        # Near miss: the same typedef name with another type changes how its headers' types resolve.
        self.assertIsNone(layered([TYPES], "typedef short s32;\nvoid alpha(void) { }\n", "alpha"))
        self.assertIsNotNone(layered([TYPES], "typedef short s16;\nvoid alpha(void) { }\n", "alpha"))

    def test_a_header_split_by_its_nested_include(self) -> None:
        nested = ("/r/include/outer.h", "struct Outer { s32 a; };\n")
        parts = {
            layers.path(TYPES[0]): layers.header_part(header_text(TYPES), Path(TYPES[0])),
            layers.path(nested[0]): layers.header_part(header_text(nested, TYPES), Path(nested[0])),
        }
        marked = (
            BOUNDARY
            + marker(str(SOURCE), 1, "1")
            + marker(nested[0], 1, "1")
            + marker(TYPES[0], 1, "1")
            + TYPES[1]
            + marker(nested[0], 2, "2")
            + nested[1]
            + marker(str(SOURCE), 2, "2")
            + "void alpha(struct Outer *o) { }\n"
        )
        part = layers.source_part(marked, SOURCE, parts, {})
        assert part is not None
        _, definition = layers.assemble(
            layers.Context(parts), part, SOURCE, "", lambda kind: {"kind": kind}, frozenset()
        )
        self.assertEqual(list(definition["aliases"]), ["s32", "f32"])
        self.assertEqual(list(definition["structs"]), ["Outer"])


class DependencyTests(unittest.TestCase):
    def test_own_layouts_read_the_header_layouts_they_name(self) -> None:
        context = layers.Context(header_parts([TYPES, SHAPES]))
        runs = [[str(SOURCE), 1], [TYPES[0], 1], [str(SOURCE), 2], [SHAPES[0], 1], [str(SOURCE), 3]]
        cases = [
            (["Shape"], ["Shape"]),
            (["Pair", "Shape"], ["Shape"]),
            # Near misses: no names, or names no header layout carries.
            ([], []),
            (["s32", "Pair"], []),
        ]
        for named, expected in cases:
            with self.subTest(named=named):
                self.assertEqual(sorted(layers.dependencies(context, runs, SOURCE, named)), expected)


class StaleFactsTests(unittest.TestCase):
    """The c3a505a/e981d8c family: cached source facts never decide which header declarations a consumer
    sees. A source part holds no header declaration; each solve reads them from the current header parts."""

    def consumed(self, data: str, body: str, part: dict | None = None) -> tuple[set[str], dict]:
        headers = [TYPES, (DATA, "\n" + data)]
        parts = header_parts(headers)
        marked, _ = unit(headers, body)
        if part is None:
            part = layers.source_part(marked, SOURCE, parts, {})
        assert part is not None
        consumed, _ = layers.assemble(
            layers.Context(parts), part, SOURCE, body, lambda kind: {"kind": kind}, frozenset()
        )
        return set(consumed["globals"]), part

    def test_a_dropped_declaration_returns_without_extracting_the_source_again(self) -> None:
        declared = "extern s32 D_800CB420_de;\nextern s32 unused_global;\n"
        dropped = "extern s32 unused_global;\n"
        cases = [
            # (source body, consumed with the declaration, consumed after the header dropped it)
            ("void alpha(void) { D_800CB420_de = 1; }\n", {"D_800CB420_de"}, set()),
            # Near misses: a source that does not use it, or only names it in a comment.
            ("void alpha(void) { }\n", set(), set()),
            ("/* D_800CB420_de */\nvoid alpha(void) { }\n", set(), set()),
        ]
        for body, with_declaration, without in cases:
            with self.subTest(body=body):
                first, part = self.consumed(declared, body)
                self.assertEqual(first, with_declaration)
                # The same cached source part, read against the header that dropped the declaration, then
                # against the header that declares it again.
                self.assertEqual(self.consumed(dropped, body, part)[0], without)
                self.assertEqual(self.consumed(declared, body, part)[0], with_declaration)


class GeneratedEvidenceTests(unittest.TestCase):
    """A header the solver wrote is its own output: it never returns as published evidence."""

    GEN = (
        "/r/include/generated.h",
        "\ntypedef short s16;\nextern s16 g_x;\nstruct A { s16 f; };\ntypedef struct A A;\n",
    )
    BODY = "struct B { struct A inner; };\nvoid alpha(void) { g_x = 1; struct A a; }\n"

    def assembled(self, header: tuple[str, str], generated: frozenset[str]) -> tuple[dict, dict]:
        headers = [header]
        parts = header_parts(headers)
        marked, _ = unit(headers, self.BODY)
        part = layers.source_part(marked, SOURCE, parts, {})
        assert part is not None
        return layers.assemble(layers.Context(parts), part, SOURCE, self.BODY, lambda kind: {"kind": kind}, generated)

    def test_a_generated_header_yields_no_published_seed(self) -> None:
        consumed, _ = self.assembled(self.GEN, frozenset({layers.path(self.GEN[0])}))
        self.assertEqual(set(consumed["globals"]), set())
        self.assertNotIn("A", consumed["structs"])

    def test_the_same_declarations_in_an_authored_header_still_are(self) -> None:
        consumed, _ = self.assembled(self.GEN, frozenset({"/r/include/other.h"}))
        self.assertIn("g_x", consumed["globals"])
        self.assertIn("A", consumed["structs"])

    def test_the_definition_seed_still_sees_the_generated_struct_size(self) -> None:
        _, definition = self.assembled(self.GEN, frozenset({layers.path(self.GEN[0])}))
        self.assertEqual(definition["structs"]["B"]["size"], 2)
