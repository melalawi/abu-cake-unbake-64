"""Real map operations survive bounded construction, input reads and result serialization.

The fixture contains 16 complete DE bodies from RageWars shard 81c384590058b4952b5384d2492f20bb9,
including the blocked func_80414CCC_de, two small neighbours and thirteen large bodies. Only the global
address inventory is sliced to constants/anchors used by these bodies. No functions or evidence are renamed.
The golden transcript was frozen from aa597b6 before the transport change. Repeating the unchanged piece
fills a real pool batch without inventing a graph; worker admission remains 512,000,000 bytes.
"""

import gc
import gzip
import hashlib
import json
import pickle
import sys
import tempfile
import tracemalloc
from collections import Counter
from concurrent.futures.process import _ResultItem
from multiprocessing.reduction import ForkingPickler
from types import SimpleNamespace
from unittest.mock import patch

from tests.kit import TESTS, TempCase
from unbake import effort, pool
from unbake.typemap import closure, evidence, shards

SLICE = (TESTS / "typemap/test_closure_transport.py").parent / "fixtures" / "ragewars_closure_slice.json.gz"
BUDGET = 32 * 1024 * 1024
TRANSCRIPT = "e883d580b7bacd517e8e1b8ab95056c236373e2e7f5f1d2d8e78a883810923d2"
COUNTS = {"connect": 1690, "link": 2593, "record": 2168, "seed_prepared": 8905, "store": 209, "use": 4087}


def canonical(value):
    if isinstance(value, dict):
        return {key: canonical(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [canonical(item) for item in value]
    if isinstance(value, set):
        return sorted(canonical(item) for item in value)
    return value


def transcript(rows):
    return hashlib.sha256(json.dumps(canonical(rows), sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class FixtureCase(TempCase):
    def setUp(self):
        super().setUp()
        with gzip.open(SLICE, "rt") as source:
            fixture = json.load(source)
        functions = fixture["functions"]
        self.names = list(functions)
        signatures = evidence.abi(functions)
        addresses = {
            version: {int(key): values for key, values in rows.items()}
            for version, rows in fixture["addresses"].items()
        }
        # Use the actual lazy SQLite map input, so the trace includes decoding rather than a preloaded tree.
        writer = shards.Writer(self.root)
        for name, item in functions.items():
            for version, body in item["versions"].items():
                writer.add(name, version, body)
        writer.connection.commit()
        writer.connection.close()
        inventory = {
            name: {
                "aliases": item["aliases"],
                "versions": {v: {"address": body["address"]} for v, body in item["versions"].items()},
            }
            for name, item in functions.items()
        }
        self.shared = (
            shards.Functions(writer.temporary, inventory),
            signatures,
            {name: set(row["registers"]) for name, row in signatures.items()},
            addresses,
        )

    def job(self, names=None, index=0):
        return self.names if names is None else names, self.root / f"{index}.pickle"


class TransportTests(FixtureCase):
    def measured(self, count):
        shared_path = self.root / "shared.pickle"
        with shared_path.open("wb") as output:
            pickle.dump(self.shared, output, protocol=5)
        jobs = [self.job(index=index) for index in range(count)]
        gc.collect()
        with patch.object(pool, "_loaded", None):
            tracemalloc.start()
            try:
                # Include shared loading, SQLite decode, worker construction and the authentic sendback envelope.
                measured = pool._measured((pool._batch, (closure._function_ops, jobs, str(shared_path))))
                self.assertIsNone(measured[-1])
                wire = ForkingPickler.dumps(_ResultItem(0, result=measured))
                _, peak = tracemalloc.get_traced_memory()
                wire_size = len(wire)
                wire.release()
            finally:
                tracemalloc.stop()
        self.assertLess(peak, BUDGET, f"construction/sendback peak {peak:,} exceeds {BUDGET:,}")
        self.assertLess(wire_size, 16 * 1024)
        paths = measured[0]
        self.assertEqual(paths, [path for _, path in jobs])
        for path in paths:
            self.assertEqual(transcript(list(closure._read_ops(path))), TRANSCRIPT)
        return paths

    def test_real_piece_including_sendback_stays_under_fixed_budget(self):
        self.measured(1)

    def test_repeated_actual_data_does_not_accumulate_in_a_maximum_pool_batch(self):
        self.measured(pool.ITEMS_PER_JOB)

    def test_exact_original_operations_side_tables_order_and_bounded_input_reads(self):
        with patch.object(shards, "bodies", wraps=shards.bodies) as reads:
            path = closure._function_ops(self.shared, self.job())
        self.assertEqual([name for call in reads.call_args_list for name in call.args[1]], self.names)
        self.assertTrue(all(len(call.args[1]) <= closure.BODY_BATCH for call in reads.call_args_list))
        rows = list(closure._read_ops(path))
        self.assertEqual([row[0] for row in rows], self.names)
        self.assertEqual(Counter(op for row in rows for op, args in row[1]), COUNTS)
        self.assertEqual(transcript(rows), TRANSCRIPT)
        # The evidence and prepared identity retain their shared function string after streaming.
        arguments = next(args for row in rows for op, args in row[1] if op == "seed_prepared")
        self.assertIs(arguments[2]["function"], dict(arguments[3])["function"])

    def test_retry_overwrites_a_partial_file_and_a_missing_terminator_is_refused(self):
        job = self.job(self.names[:1])
        job[1].write_bytes(b"interrupted output")
        path = closure._function_ops(self.shared, job)
        expected = list(closure._function_rows(self.shared, job[0]))
        self.assertEqual(list(closure._read_ops(path)), expected)
        with path.open("wb") as output:
            pickle.dump(expected[0], output, protocol=5)
        with self.assertRaises(EOFError):
            list(closure._read_ops(path))
        self.assertEqual(list(closure._read_ops(closure._function_ops(self.shared, job))), expected)

    def test_actual_worker_admission_at_512mb_sends_only_paths_for_repeated_real_pieces(self):
        if sys.platform != "linux":
            self.skipTest("RLIMIT_DATA proof runs on Linux")
        jobs = [self.job(index=index) for index in range(8)]
        started = effort.mark()
        with pool.Pool(2, 2_000_000_000, 512_000_000, 512_000_000, self.root / "machine") as workers:
            paths = workers.run(closure._function_ops, jobs, self.shared)
        spent = effort.since(started)
        self.assertEqual(paths, [path for _, path in jobs])
        self.assertEqual(spent.pool[effort.name_of(closure._function_ops)][1], len(jobs))
        self.assertNotIn("worker.retry", spent.counts)
        for path in paths:
            self.assertEqual(transcript(list(closure._read_ops(path))), TRANSCRIPT)


class BuildTests(FixtureCase):
    # These smaller real bodies make graph equivalence and cleanup checks cheap.
    def setUp(self):
        super().setUp()
        names = self.names[:3]
        functions, signatures, used, addresses = self.shared
        self.shared = (
            shards.Functions(functions.path, {name: functions.inventory[name] for name in names}),
            signatures,
            used,
            addresses,
        )
        self.names = names
        self.host = SimpleNamespace(
            workers=2,
            cores=2,
            memory_total_bytes=2_000_000_000,
            memory_parent_bytes=512_000_000,
            memory_worker_bytes=512_000_000,
            cache_machine_root=self.root / "machine",
        )

    def run_inline(self, host, fn, jobs, shared=None):
        return [fn(job) if shared is None else fn(shared, job) for job in jobs]

    def test_build_preserves_graph_and_task_boundaries_under_explicit_scratch(self):
        expected = closure.build(*self.shared, log=None)
        forbidden = self.root / "unusable-system-temp"
        with (
            patch.object(tempfile, "tempdir", str(forbidden)),
            patch.object(pool, "run", side_effect=self.run_inline) as run,
            patch.object(closure, "_function_body", wraps=closure._function_body) as bodies,
        ):
            actual = closure.build(*self.shared, log=None, host=self.host)
        self.assertEqual(actual, expected)
        self.assertEqual(bodies.call_count, len(self.names))
        closure_jobs = next(call.args[2] for call in run.call_args_list if call.args[1] is closure._function_ops)
        self.assertEqual([names for names, _ in closure_jobs], shards.chunks(self.names, pool.workers(self.host)))
        self.assertTrue(all(path.parent.parent == self.host.cache_machine_root for _, path in closure_jobs))
        self.assertEqual(list(self.host.cache_machine_root.iterdir()), [])
        self.assertFalse(forbidden.exists())

    def test_private_directory_is_removed_after_worker_or_replay_failure(self):
        for failure in ("worker", "replay"):
            with self.subTest(failure=failure):

                def run(host, fn, jobs, shared=None, failure=failure):
                    result = self.run_inline(host, fn, jobs, shared)
                    if fn is closure._function_ops:
                        if failure == "worker":
                            raise ValueError("worker failed after a partial write")
                        result[0].write_bytes(b"interrupted output")
                    return result

                with (
                    patch.object(pool, "run", side_effect=run),
                    self.assertRaises((ValueError, pickle.UnpicklingError)),
                ):
                    closure.build(*self.shared, log=None, host=self.host)
                self.assertEqual(list(self.host.cache_machine_root.iterdir()), [])
