"""Fold's parent-side work is counted: one context parse, one catalog per text, pooled and cached."""

import tempfile
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from tests.fold import test_provider_reuse
from tests.project_fixture import ProjectCase
from unbake.fold import provider_reuse
from unbake.layout import header_context
from unbake.layout.header_context import Headers


class LazyContextTests(TestCase):
    def test_contexts_replaced_before_use_are_never_parsed(self):
        texts = {Path("/a/one.h"): "typedef int one;\n"}
        real = header_context.context
        with patch.object(header_context, "context", side_effect=real) as parse:
            built = [Headers({**texts, Path(f"/a/n{i}.h"): f"typedef int n{i};\n"}, root=None) for i in range(5)]
            self.assertEqual(len(built[-1].texts), 2)
            self.assertEqual(parse.call_count, 0)
            self.assertEqual(len(built[-1].records), 0)
            self.assertIn("one", built[-1].types)
            self.assertEqual(parse.call_count, 1)


class CatalogWorkTests(TestCase):
    def test_each_distinct_text_is_catalogued_once_and_pooled(self):
        texts = [f"typedef int t{i};\n" for i in range(130)]
        memo: dict = {}
        jobs = []

        def run(host, fn, items, shared=None):
            jobs.append(len(items))
            return [fn(shared, item) for item in items]

        with (
            patch("unbake.pool.run", side_effect=run),
            patch.object(provider_reuse, "_catalog", side_effect=provider_reuse._catalog) as parsed,
        ):
            provider_reuse.catalogs_of([*texts, *texts], object(), None, memo)
            self.assertEqual(parsed.call_count, 130)
            self.assertEqual(jobs, [3])
            provider_reuse.catalogs_of(texts, object(), None, memo)
            self.assertEqual(parsed.call_count, 130)
            self.assertEqual(jobs, [3])

    def test_a_second_run_reads_the_project_cache(self):
        texts = [f"typedef int t{i};\n" for i in range(5)]
        with (
            tempfile.TemporaryDirectory() as root,
            patch.object(provider_reuse, "_catalog", side_effect=provider_reuse._catalog) as parsed,
        ):
            provider_reuse._catalog_job(Path(root), texts)
            self.assertEqual(parsed.call_count, 5)
            again = provider_reuse._catalog_job(Path(root), texts)
            self.assertEqual(parsed.call_count, 5)
            self.assertEqual(len(again), 5)


class PlanWorkTests(ProjectCase):
    contents = test_provider_reuse.ProviderReuseTests.contents

    def test_the_include_graph_is_built_once_for_all_consumers(self):
        view, contents = self.contents()
        with patch.object(provider_reuse.Graph, "contents", wraps=provider_reuse.Graph.contents) as graphs:
            edits = provider_reuse.plan(view, contents, ("us", "eu"))
        self.assertGreaterEqual(len(edits), 2)
        self.assertEqual(graphs.call_count, 1)
