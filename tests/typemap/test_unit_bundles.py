"""A solve dispatches only physical sources whose complete layered facts changed."""

from unittest.mock import patch

from tests.project_fixture import ProjectCase
from unbake import pool
from unbake.cache import Cache
from unbake.typemap import declarations, facts


class UnitBundleTests(ProjectCase):
    versions = ("us",)

    def setUp(self):
        super().setUp()
        self.sources = [self.project.src / (name + ".c") for name in ("alpha", "beta")]
        for source in self.sources:
            source.write_text('#include "types.h"\nint ' + source.stem + "(void) { return 1; }\n")
        self.tasks = [(source.stem, source, "us") for source in self.sources]
        self.calls = []

    def run_inline(self, host, fn, jobs, shared=None):
        return [fn(job) if shared is None else fn(shared, job) for job in jobs]

    def unit(self, shared, versions):
        self.calls.append(versions[0][0][2][1].stem)
        output = facts.Store(self.project, Cache(self.project.cache))
        rows = [
            (index, output.encoded([{"functions": {function: {"return": "int"}}, "aliases": {}}]))
            for group in versions
            for index, _, (function, _, _) in group
        ]
        return rows, {"sources": 1, "whole": 0}

    def collect(self):
        with (
            patch.object(declarations, "published_sources", return_value=self.tasks),
            patch.object(pool, "run", self.run_inline),
            patch.object(facts, "_unit_job", self.unit),
            patch.object(facts, "_header_job", return_value=0),
            patch.object(facts.Snapshot, "generated", return_value=frozenset({self.project.include[0] / "types.h"})),
        ):
            keys = facts.published_keys(self.project, self.host)
            return facts.published(self.project, self.host, facts.Store(self.project, Cache(self.project.cache)), keys)

    def test_warm_solve_skips_workers_and_one_landing_dispatches_only_its_source(self):
        first = self.collect()
        self.assertEqual(self.calls, ["alpha", "beta"])
        self.calls.clear()
        self.assertEqual(self.collect(), first)
        self.assertEqual(self.calls, [])
        self.sources[0].write_text(self.sources[0].read_text() + "/* landed body */\n")
        self.assertEqual(self.collect(), first)
        self.assertEqual(self.calls, ["alpha"])

    def test_header_contract_changes_invalidate_includers_even_with_stable_unit_keys(self):
        self.collect()
        self.calls.clear()
        header = self.project.include[0] / "types.h"
        header.write_text(header.read_text() + "extern int new_contract;\n")
        self.collect()
        self.assertEqual(self.calls, ["alpha", "beta"])

    def test_new_logical_owner_reindexes_cached_results_without_changing_order(self):
        first = self.collect()
        self.calls.clear()
        source = self.project.src / "aardvark.c"
        source.write_text("int aardvark(void) { return 0; }\n")
        self.tasks.insert(0, ("aardvark", source, "us"))
        results = self.collect()
        self.assertEqual(results[1:], first)
        self.assertEqual(self.calls, ["aardvark"])

    def test_bundle_keys_are_derived_by_the_pool_not_the_parent(self):
        names = []
        original = self.run_inline

        def run(host, fn, jobs, shared=None):
            names.append(getattr(fn, "__name__", ""))
            return original(host, fn, jobs, shared)

        self.run_inline = run
        self.collect()
        self.assertIn("_bundle_job", names)
