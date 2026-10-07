"""Bounded decoding of real named-Acmd facts, including the pool's result serialization.

The fixture keeps both seeds, contracts, shared references and selected layouts from the first entry of
RageWars facts-unit 09115905e4c367ca8852eca3b10d53f2975fc273f68659f2e09c71eb2a22a7b4.
Only the struct maps were sliced, retaining World, Settings580, RulesAC_2, Acmd, Awords, Gfx and OS layouts.
Repeating these unchanged bytes fills the existing 256-entry decode job without a large fixture or solve.
"""

import gc
import json
import tracemalloc
from multiprocessing.reduction import ForkingPickler
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tests.kit import TempCase
from unbake import pool
from unbake.typemap import declarations, facts, facts_decode

SLICE = Path(__file__).parent / "fixtures" / "ragewars_decode_slice.json"
BUDGET = 2 * 1024 * 1024


class DecodeMemoryTests(TempCase):
    def setUp(self):
        super().setUp()
        self.payload = SLICE.read_bytes()

    def measured(self, job):
        gc.collect()
        tracemalloc.start()
        try:
            decoded = facts._decode_job(job)
            # Route 5 failed here, after the action returned: include the real pool sendback pickler.
            wire = ForkingPickler.dumps(decoded)
            _, peak = tracemalloc.get_traced_memory()
            wire_size = len(wire)
            wire.release()
        finally:
            tracemalloc.stop()
        self.assertEqual([index for index, _ in decoded], [index for index, _ in job])
        self.assertLess(peak, BUDGET, f"decode/sendback peak {peak:,} bytes exceeds {BUDGET:,}")
        return decoded, wire_size

    def test_one_real_decode_job_stays_under_fixed_budget(self):
        decoded, _ = self.measured([(index, self.payload) for index in range(256)])
        self.assertEqual(len(decoded), 256)
        self.assertEqual(facts_decode.read(decoded[0][1]), json.loads(self.payload))

    def test_file_job_keeps_input_and_output_trees_out_of_the_pool_transport(self):
        job = facts_decode.jobs(dict.fromkeys(range(256), self.payload), 256, self.root)[0]
        decoded, wire_size = self.measured(job)
        self.assertLess(len(ForkingPickler.dumps(job)), 64 * 1024)
        self.assertLess(wire_size, 64 * 1024)
        expected = json.loads(self.payload)
        self.assertEqual(facts_decode.read(decoded[0][1]), expected)
        self.assertEqual(facts_decode.read(decoded[-1][1]), expected)
        # A retry overwrites private partial output, and still reads the same input.
        Path(decoded[0][1]).write_bytes(b"interrupted output")
        self.assertEqual(facts_decode.read(facts._decode_job(job[:1])[0][1]), expected)


class PublishedDecodeTests(TempCase):
    def setUp(self):
        super().setUp()
        self.payload = SLICE.read_bytes()
        self.source = self.root / "unit.c"
        self.source.write_text("int unit(void);\n")
        self.project = SimpleNamespace(root=self.root)
        self.tasks = [(f"owner_{index}", self.source, "de") for index in range(513)]
        self.jobs = []
        self.fail = None

    def unit(self, shared, versions):
        return (
            [(index, self.payload) for group in versions for index, _, _ in group],
            {"sources": 1, "whole": 0},
        )

    def run_inline(self, host, fn, jobs, shared=None):
        if fn is facts._decode_job:
            self.jobs = jobs
            if self.fail == "worker":
                raise ValueError("worker failure")
        return [fn(job) if shared is None else fn(shared, job) for job in jobs]

    def collect(self, policy):
        output = facts.Store(self.project, None)
        shared_value = {"s32": "int"}
        with (
            patch.object(declarations, "published_sources", return_value=self.tasks),
            patch.object(facts.Snapshot, "generated", return_value=frozenset()),
            patch.object(facts, "_headers", return_value=[]),
            patch.object(facts, "_unit_job", self.unit),
            patch.object(pool, "run", self.run_inline),
            patch.object(output, "_get_shared", return_value=shared_value) as resolve,
        ):
            result = facts.published(self.project, policy, output, ["key"] * len(self.tasks))
        self.assertEqual(resolve.call_count, len(self.tasks) * 4)
        self.assertTrue(all(seed["aliases"] is shared_value for seed in result))
        return result

    def test_published_keeps_job_count_boundaries_order_and_seed_content(self):
        policy = SimpleNamespace(cache_machine_root=self.root)
        result = self.collect(policy)
        self.assertEqual([len(job) for job in self.jobs], [256, 256, 1])
        self.assertEqual([index for job in self.jobs for index, _ in job], list(range(513)))
        self.assertTrue(all(isinstance(data, Path) for job in self.jobs for _, data in job))
        self.assertTrue(all(not path.parent.exists() for job in self.jobs for _, path in job))
        expected = json.loads(self.payload)
        for index, seed in enumerate(result):
            original = expected[index % len(expected)]
            for name in ("functions", "globals", "structs", "arrays", "unknown"):
                self.assertEqual(seed[name], original[name])
        self.assertEqual(self.collect(None), result)

    def test_private_transport_is_cleaned_after_worker_or_parent_failure(self):
        policy = SimpleNamespace(cache_machine_root=self.root)
        for failure in ("worker", "parent"):
            with self.subTest(failure=failure):
                self.fail = failure
                reader = (
                    facts_decode.read
                    if failure == "worker"
                    else lambda data: (_ for _ in ()).throw(ValueError("parent failure"))
                )
                with patch.object(facts_decode, "read", reader), self.assertRaisesRegex(ValueError, failure):
                    self.collect(policy)
                self.assertTrue(self.jobs)
                self.assertTrue(all(not path.parent.exists() for job in self.jobs for _, path in job))
