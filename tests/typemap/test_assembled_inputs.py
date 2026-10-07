"""An assembled hit avoids header layout work; changed header identities still change real facts."""

import json
from types import SimpleNamespace
from unittest.mock import patch

from tests.kit import TempCase
from tests.typemap.test_facts import fixture_graph
from unbake.project.headers import Graph
from unbake.typemap import declarations, facts, layers


class AssembledInputTests(TempCase):
    def test_warm_facts_skip_dependency_reconstruction_and_changed_layout_is_observed(self):
        include = self.root / "include"
        include.mkdir()
        header = include / "shared.h"
        source = self.root / "alpha.c"
        source.write_text('#include "shared.h"\nstruct Local { struct Shared value; };\nint alpha(void) {return 0;}\n')
        project = SimpleNamespace(
            root=self.root, include=(include,), build=self.root / "build", version=lambda v: SimpleNamespace(macros=())
        )
        output = facts.Store(project, None)
        spell = facts._spell(project, None)

        def run(header_text):
            header.write_text(header_text)
            text = (
                declarations.BOUNDARY
                + f'\n# 1 "{header}"\n'
                + header_text
                + f'\n# 1 "{source}"\n'
                + source.read_text().split("\n", 1)[1]
            )
            snapshot = fixture_graph(project)
            identity = facts.header_key(project, None, header, "us", snapshot)
            output.put_json(facts.HEADER, identity, layers.header_part(text.split(f'# 1 "{source}"')[0], header, spell))
            parts = {"us": facts._Parts(output, {spell(str(header)): identity})}
            content = facts.unit_key(project, None, source, "us", snapshot)
            with patch.object(declarations, "source_unit", return_value=text):
                rows = facts._source_tasks(
                    project,
                    None,
                    output,
                    parts,
                    {},
                    [(0, content, ("alpha", source, "us"))],
                    {"sources": 0, "whole": 0},
                    {},
                    snapshot,
                    frozenset(),
                )
            return rows

        with patch.object(Graph, "generated", return_value=frozenset()):
            first = run("struct Shared { int a; };\n")
            with patch.object(
                layers, "dependencies", side_effect=AssertionError("cached facts rebuilt header layouts")
            ):
                self.assertEqual(run("struct Shared { int a; };\n"), first)
            changed = run("struct Shared { int a; int b; };\n")
        self.assertIsNotNone(first)
        self.assertNotEqual(first, changed)
        before = output.decode(json.loads(first[0][1])[1])["structs"]["Local"]["size"]
        after = output.decode(json.loads(changed[0][1])[1])["structs"]["Local"]["size"]
        self.assertEqual((before, after), (4, 8))

    def test_added_generated_include_refreshes_runs_without_reparsing_source(self):
        include = self.root / "include"
        include.mkdir()
        first = include / "first.h"
        extra = include / "extra.h"
        first.write_text("typedef int Used;\n")
        extra.write_text("struct Added { int field; };\n")
        source = self.root / "alpha.c"
        source.write_text('#include "first.h"\nUsed alpha(void) { return 0; }\n')
        project = SimpleNamespace(
            root=self.root, include=(include,), build=self.root / "build", version=lambda v: SimpleNamespace(macros=())
        )
        output = facts.Store(project, None)
        spell = facts._spell(project, None)
        generated = frozenset({first, extra})

        def run(added):
            snapshot = fixture_graph(project)
            parts = {}
            for header in generated:
                text = declarations.BOUNDARY + f'\n# 1 "{header}"\n' + header.read_text()
                content = facts.header_key(project, None, header, "us", snapshot)
                output.put_json(facts.HEADER, content, layers.header_part(text, header, spell))
                parts[spell(str(header))] = content
            text = declarations.BOUNDARY + f'\n# 1 "{first}"\ntypedef int Used;\n'
            if added:
                text += f'# 1 "{extra}"\n' + extra.read_text()
            text += f'# 2 "{source}"\nUsed alpha(void) {{ return 0; }}\n'
            content = facts.unit_key(project, None, source, "us", snapshot)
            with patch.object(declarations, "source_unit", return_value=text):
                rows = facts._source_tasks(
                    project,
                    None,
                    output,
                    {"us": facts._Parts(output, parts)},
                    {},
                    [(0, content, ("alpha", source, "us"))],
                    {"sources": 0, "whole": 0},
                    {},
                    snapshot,
                    frozenset(spell(str(header)) for header in generated),
                )
            return content, rows

        with patch.object(Graph, "generated", return_value=generated):
            before, rows = run(False)
            self.assertNotIn("Added", output.decode(json.loads(rows[0][1])[1])["structs"])
            first.write_text('#include "extra.h"\ntypedef int Used;\n')
            with patch.object(layers, "source_part", side_effect=AssertionError("source reparsed")):
                after, rows = run(True)
            self.assertEqual(before, after)
            self.assertIn("Added", output.decode(json.loads(rows[0][1])[1])["structs"])
