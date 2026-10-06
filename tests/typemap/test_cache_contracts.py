"""Cached semantic outputs must change when an actual compilation dependency changes."""

from unittest.mock import patch

from tests.preprocessor import output
from tests.project_fixture import ProjectCase
from tests.typemap.test_facts import SourceKeyTests
from tests.typemap.test_render_once import session, solution
from unbake import process
from unbake.cache import Cache
from unbake.typemap import database, facts, regeneration


class EffectiveDependencyTests(SourceKeyTests):
    def keys(self):
        snapshot = facts.Snapshot(self.project)
        return (
            facts.source_key(self.project, self.host, ("alpha", self.source, "us"), snapshot),
            facts.unit_key(self.project, self.host, self.source, "us", snapshot),
        )

    def test_forced_header_and_macro_file_bytes_are_inputs(self):
        forced = self.root / "forced.h"
        for flag in ("-include", "-imacros"):
            with self.subTest(flag=flag):
                self.project.unit_flags = {"alpha": (flag, str(forced))}
                forced.write_text("#define COUNT 1\n")
                before = self.keys()
                forced.write_text("#define COUNT 2\n")
                after = self.keys()
                self.assertTrue(all(a != b for a, b in zip(before, after, strict=True)))

    def test_unit_include_search_pins_the_selected_header_and_its_nested_dependencies(self):
        extra = self.root / "extra"
        extra.mkdir()
        self.source.write_text('#include "options.h"\nint alpha(void) { return COUNT; }\n')
        (extra / "options.h").write_text('#include "nested.h"\n')
        for flags in (("-I" + str(extra),), ("-I", str(extra)), ("-iquote", str(extra)), ("-isystem", str(extra))):
            with self.subTest(flags=flags):
                self.project.unit_flags = {"alpha": flags}
                (extra / "nested.h").write_text("#define COUNT 1\n")
                before = self.keys()
                (extra / "nested.h").write_text("#define COUNT 2\n")
                self.assertTrue(all(a != b for a, b in zip(before, self.keys(), strict=True)))


class RenderInputTests(ProjectCase):
    versions = ("us",)

    def test_caller_argument_changes_regenerate_a_void_header_contract(self):
        current = session(Cache(self.root / "render-cache"), "stable inputs")
        value = solution()
        value["functions"]["f"]["abi"] = {"caller_arguments": []}
        path = self.project.include[0] / "f.h"

        def compute():
            value.update(declaration_headers={"f": "f.h"}, shared_aliases={})
            names = database.unprototyped_calls(value["functions"])
            return {path: database.without_void("void f(void);", database.void_calls(names)).encode()}

        with patch.object(regeneration.layout_index, "load", return_value={"headers": {}}):
            first = current.render(value, compute)
            value["functions"]["f"]["abi"]["caller_arguments"] = ["r4"]
            second = current.render(value, compute)
        self.assertEqual(first[path], b"void f(void);")
        self.assertEqual(second[path], b"void f();")

    def test_header_macro_edit_refreshes_cached_local_ownership(self):
        header = self.project.include[0] / "types.h"
        source = self.project.src / "alpha.c"
        source.write_text(
            '#include "types.h"\n#if PRIVATE\ntypedef int Local;\n#endif\nint alpha(void) { return 0; }\n'
        )
        with patch.object(process.subprocess, "run", side_effect=output):
            header.write_text("#define PRIVATE 0\n")
            first = regeneration.Session(self.project, self.host)
            header.write_text("#define PRIVATE 1\n")
            second = regeneration.Session(self.project, self.host)
        self.assertNotIn("Local", first.projections[source]["owned"])
        self.assertIn("Local", second.projections[source]["owned"])


class PhysicalSourceKeyTests(ProjectCase):
    def test_aliases_of_one_physical_source_do_not_repeat_version_key_work(self):
        source = self.project.src / "alpha.c"
        source.write_text("int alpha(void) { return 1; }\nint alias(void) { return 2; }\n")
        tasks = [(name, source, version) for name in ("alpha", "alias") for version in self.project.versions]
        expected = [
            facts.unit_key(self.project, self.host, path, version, facts.Snapshot(self.project))
            for _, path, version in tasks
        ]
        real = facts.unit_key
        calls = []

        def keyed(project, host, path, version, snapshot):
            calls.append((path, version))
            return real(project, host, path, version, snapshot)

        def inline(host, function, items, shared):
            return [function(shared, item) for item in items]

        with (
            patch.object(facts.declarations, "published_sources", return_value=tasks),
            patch.object(facts, "unit_key", keyed),
            patch("unbake.pool.run", side_effect=inline),
        ):
            found = facts.published_keys(self.project, self.host)
        self.assertEqual(found, expected)
        self.assertEqual(len(calls), len(self.project.versions))

    def test_version_macros_do_not_rewalk_an_identical_include_search(self):
        source = self.project.src / "alpha.c"
        source.write_text('#include "types.h"\nint alpha(void) { return 0; }\n')
        snapshot = facts.Snapshot(self.project)
        with patch.object(snapshot, "edges", wraps=snapshot.edges) as edges:
            first = facts.unit_key(self.project, self.host, source, "us", snapshot)
            visits = edges.call_count
            second = facts.unit_key(self.project, self.host, source, "eu", snapshot)
            repeated = edges.call_count
        self.assertNotEqual(first, second)
        self.assertGreater(visits, 0)
        self.assertEqual(repeated, visits)
