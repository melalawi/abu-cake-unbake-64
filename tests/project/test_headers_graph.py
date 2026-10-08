"""Exact search, immutable overlays and negative probes on real RW header payloads."""

from pathlib import Path
from unittest.mock import patch

from tests.project_fixture import ProjectCase
from unbake import cache
from unbake.config import Held
from unbake.project.headers import Graph, Include, Search, TreeView, scan

REAL = Path(__file__).parents[1] / "fixtures/ragewars_declaration_order/vec.h"


class GraphTests(ProjectCase):
    versions = ("us",)

    def test_quote_angle_search_order_forced_inputs_and_shadow_creation(self):
        root = self.project.root
        local, quote, ordinary, system = (root / n for n in ("local", "quote", "ordinary", "system"))
        for directory in (local, quote, ordinary, system):
            directory.mkdir()
        files = {directory / "same.h": REAL.read_bytes() for directory in (quote, ordinary, system)}
        source = local / "source.c"
        files[source] = b'#include "same.h"\n#include <same.h>\n'
        view = TreeView.contents(files, (ordinary,), root=root)
        search = Search.command(
            root,
            [
                "cpp",
                "-iquote",
                str(quote),
                "-I",
                str(ordinary),
                "-isystem",
                str(system),
                "-include",
                "same.h",
                "-imacros",
                "same.h",
                "-DCOUNT=3",
            ],
            (),
        )
        graph = Graph(view, search)
        quoted, angled = scan(files[source].decode())
        self.assertEqual(graph.resolve(source, quoted).target, quote / "same.h")
        self.assertEqual(graph.resolve(source, angled).target, ordinary / "same.h")
        self.assertEqual(graph.resolve(source, quoted).probes[0].state, "missing")
        after = Graph(view.overlay({local / "same.h": REAL.read_bytes()}), search)
        self.assertEqual(after.resolve(source, quoted).target, local / "same.h")
        self.assertNotEqual(
            graph.closure((source,)).dependency_set.digest, after.closure((source,)).dependency_set.digest
        )
        self.assertEqual(search.macros, ("-DCOUNT=3",))
        self.assertEqual(search.forced, ("same.h", "same.h"))

    def test_comments_splices_unknown_edges_cycle_and_overlay_immutability(self):
        root = self.project.include[0]
        a, b = root / "a.h", root / "b.h"
        files = {a: b'/* #include "fake.h" */\n#include \\\n"b.h"\n', b: REAL.read_bytes() + b'\n#include "a.h"\n'}
        view = TreeView.contents(files, (root,))
        graph = Graph(view, Search(include_roots=(root,)))
        self.assertEqual(set(graph.closure((a,)).paths), {a, b})
        self.assertEqual(len(graph.edges(a)), 1)
        with self.assertRaises(TypeError):
            view.files[a] = b"mutated"
        self.assertEqual(view.overlay({b: None}).files.keys(), {a})
        self.assertEqual(view.read(b), files[b])
        unknown = Graph(view.overlay({a: b"#include HEADER_NAME\n"}), graph.search)
        self.assertTrue(unknown.closure((a,)).unknown)
        self.assertEqual(unknown.resolve(a, Include("macro", False, 0, 0, True)).target, None)

    def test_conditional_include_of_an_existing_path_is_known_and_a_missing_one_names_its_search(self):
        root = self.project.include[0]
        (root / "span").mkdir(exist_ok=True)
        header, types, absent = root / "span/code.h", root / "types.h", root / "span/absent.h"
        view = TreeView.contents(
            {
                header: b'#ifndef TYPES_H\n#include "../types.h"\n#endif\n',
                types: REAL.read_bytes(),
                absent: b'#ifdef X\n#include "gone.h"\n#endif\n',
            },
            (root,),
        )
        graph = Graph(view, Search(include_roots=(root,)))
        found = graph.closure((header,))
        self.assertFalse(found.unknown)
        self.assertIn(types, found.paths)
        missing = graph.closure((absent,))
        self.assertTrue(missing.unknown)
        self.assertEqual(len(missing.unresolved), 1)
        self.assertIn("'gone.h'", missing.unresolved[0])
        self.assertIn("span/gone.h", missing.unresolved[0])
        self.assertIn("gone.h", missing.unresolved[0].split("searched")[1])

    def test_projection_recipe_invalidation_and_explicit_parse_failure(self):
        path = self.project.include[0] / "real.h"
        graph = Graph.contents({path: REAL.read_bytes()}, self.project.include)
        row = graph.projection(path)
        self.assertIsNone(row.parse_error)
        self.assertTrue(row.typedefs | row.tags)
        bad = Graph.contents({path: "typedef int Broken("}, self.project.include)
        self.assertIsNotNone(bad.projection(path).parse_error)
        with self.assertRaisesRegex(Held, "headers.projection"):
            bad.providers(["Broken"])
        with patch(
            "unbake.project.headers.project", wraps=__import__("unbake.project.headers", fromlist=["project"]).project
        ) as parsed:
            graph2 = Graph.contents({path: REAL.read_bytes()}, self.project.include)
            graph2._recipe = "changed-parser-recipe"
            graph2.projection(path)
        self.assertEqual(parsed.call_count, 1)
        self.assertLessEqual(cache.resident_bytes(), 1 << 20)

    def test_real_ast_accounting_stops_after_root_and_ext_without_cloning(self):
        import sys

        from unbake import cdecl

        source = REAL.with_name("scalar.h").read_text() + REAL.read_text()
        tree = cdecl.parse(cdecl.declaration_source(source))
        self.assertGreater(len(tree.ext), 0)
        limit = sys.getsizeof(tree)
        with patch.object(cache.sys, "getsizeof", wraps=cache.sys.getsizeof) as scanned:
            weight = cache.memory_size(tree, limit=limit)
        self.assertEqual(weight, limit + 1)
        self.assertEqual(scanned.call_count, 2)
