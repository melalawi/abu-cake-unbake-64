"""Bound pending work and discard candidate parsing while retaining ordered proofs."""

import tempfile
import unittest
from concurrent.futures import Future
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from unbake.layout.header_context import Headers
from unbake.match import batch_fold, declarations, forked
from unbake.project import cache
from unbake.project.config import Held


class PendingTests(unittest.TestCase):
    def test_order_and_early_close_table(self):
        for count, workers, consume in [(0, 2, 0), (1, 2, 1), (20, 2, 20), (20, 2, 1)]:
            with self.subTest(count=count, workers=workers, consume=consume):
                submitted = []
                futures = []

                def submit(fn, item, submitted=submitted, futures=futures):
                    submitted.append(item)
                    future = Future()
                    future.set_result(fn(item))
                    futures.append(future)
                    return future

                pool = SimpleNamespace(submit=submit)
                stream = forked._bounded(pool, lambda item: item * 2, list(range(count)), workers)
                values = []
                for _ in range(consume):
                    values.append(next(stream))
                    self.assertLessEqual(len(submitted) - len(values), workers)
                stream.close()
                self.assertEqual(values, [i * 2 for i in range(consume)])
                self.assertLessEqual(len(submitted), consume + workers)

    def test_cancel_queued_work_before_snapshot_is_removed(self):
        first = Future()
        first.set_result(1)
        queued = Future()
        pool = SimpleNamespace(submit=Mock(side_effect=[first, queued]))
        stream = forked._bounded(pool, lambda item: item, [1, 2, 3], 2)
        self.assertEqual(next(stream), 1)
        stream.close()
        self.assertTrue(queued.cancelled())
        self.assertEqual(pool.submit.call_count, 2)

    def test_candidate_state_is_released_on_success_and_failure(self):
        for fail in (False, True):
            with self.subTest(fail=fail), patch.object(forked, "release") as release:

                def work(shared, item, fail=fail):
                    if fail:
                        raise Held("fold", "candidate failure")
                    return shared + item

                if fail:
                    with self.assertRaises(Held):
                        forked._run(work, 2, 3)
                else:
                    self.assertEqual(forked._run(work, 2, 3), ([], 5))
                release.assert_called_once_with(shared=True)

    def test_shared_candidate_collects_young_objects_without_clearing_context(self):
        for shared in (True, False):
            with (
                self.subTest(shared=shared),
                patch.object(cache, "_parsed", {}),
                patch.object(cache, "_remembered", {}),
                patch.object(batch_fold.type_rewrite, "_context") as context,
                patch.object(forked.gc, "collect") as collect,
            ):
                forked.release(shared=shared)
                if shared:
                    collect.assert_called_once_with(0)
                    context.cache_clear.assert_not_called()
                else:
                    collect.assert_called_once_with()
                    context.cache_clear.assert_called_once_with()

    def test_release_keeps_disk_artifacts_and_clears_memory(self):
        with patch.object(cache, "_parsed", {"source": object()}), patch.object(cache, "_remembered", {"context": {}}):
            with patch.object(batch_fold.type_rewrite, "_context") as context:
                forked.release()
            self.assertEqual(cache._parsed, {})
            self.assertEqual(cache._remembered, {})
            context.cache_clear.assert_called_once_with()

    def test_parsed_objects_have_a_fixed_bound_and_edits_are_observed(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(cache, "_parsed", {}):
            root = Path(directory)
            paths = [root / str(i) for i in range(100)]
            for path in paths:
                path.write_text(path.name)
                self.assertEqual(cache.parsed("object", path, path.read_text), path.name)
            self.assertLessEqual(len(cache._parsed), 64)
            paths[-1].write_text("changed")
            self.assertEqual(cache.parsed("object", paths[-1], paths[-1].read_text), "changed")

    def test_finish_releases_worker_snapshots_before_feedback(self):
        for active in (None, (SimpleNamespace(shutdown=Mock()), Path("snapshot-directory"))):
            with self.subTest(active=active), patch.object(forked, "release") as release:
                token = forked._pool.set(active)
                try:
                    forked.finish()
                    self.assertIsNone(forked._pool.get())
                    if active is not None:
                        active[0].shutdown.assert_called_once_with(cancel_futures=True)
                    release.assert_called_once_with()
                finally:
                    forked._pool.reset(token)


class StreamingFoldTests(unittest.TestCase):
    def test_fold_is_lazy_and_adopts_headers_before_yielding(self):
        root = Path("project")
        headers = Headers({}, root=root)
        candidates = [SimpleNamespace(function=name) for name in ("alpha", "beta")]
        calls = []

        def folded(staged, policy, headers, candidate, changes):
            calls.append(candidate.function)
            return declarations.Folded(candidate.function, candidate.function, [], {})

        with (
            patch.object(batch_fold, "_warm_contexts"),
            patch.object(
                batch_fold.forked,
                "ordered",
                side_effect=lambda fn, shared, members, cores: iter(([], batch_fold.Trial("again")) for _ in members),
            ),
            patch.object(batch_fold, "_fold_one", side_effect=folded),
            patch.object(forked, "release"),
        ):
            stream = batch_fold.fold(None, SimpleNamespace(cores=12), headers, candidates, [])
            self.assertEqual(calls, [])
            self.assertEqual(next(stream)[0].function, "alpha")
            self.assertEqual(calls, ["alpha"])
            self.assertEqual([c.function for c, _ in stream], ["beta"])
