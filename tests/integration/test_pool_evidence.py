"""Real child crashes and refusals must preserve retry and public effort contracts."""

import os
import resource
import tempfile
import time
import unittest
from pathlib import Path

from tests.ledger_fixture import fault_evidence
from unbake import effort, pool
from unbake.config import Held
from unbake.cycle.engine import _result
from unbake.process import named

CAP = 512000000


def burn():
    start = time.process_time()
    while time.process_time() - start < 0.04:
        sum(range(1000))
    effort.count("integration.work", 1, 1)


def refused(value):
    burn()
    raise Held(
        named(
            "integration.refused", "integration.refused: measured child failure", owner="fixture", stage="integration"
        )
    )


def allocation(value):
    burn()
    bytearray(CAP * 2)


def once(marker):
    path = Path(marker)
    if not path.exists():
        path.write_text(str(os.getpid()))
        os._exit(7)
    return {"pid": os.getpid(), "cap": resource.getrlimit(resource.RLIMIT_DATA)[0]}


def always(value):
    burn()
    os._exit(7)


class PoolEvidenceTests(unittest.TestCase):
    def workers(self):
        return pool.Pool(10, 16 << 30, 4 << 30, CAP)

    def test_real_crash_retries_in_new_worker_at_same_cap(self):
        with tempfile.TemporaryDirectory() as directory, self.workers() as workers:
            marker = str(Path(directory) / "crash")
            (row,) = workers.map(once, [marker])
            self.assertNotEqual(row["pid"], int(Path(marker).read_text()))
            self.assertEqual(row["cap"], CAP)

    def test_measured_child_refusal_reaches_cycle_result_with_wall_cpu_and_counts(self):
        before = effort.counted().get("integration.work", (0, 0))
        with self.workers() as workers:
            future = workers.submit(refused, None)
            with self.assertRaises(Held):
                future.result()
            row = _result(future)
        self.assertEqual(row["key"], "integration.refused")
        self.assertGreaterEqual(row["seconds"], 0.03)
        self.assertGreaterEqual(row["fault"]["chain"][0]["fault"]["cpu_seconds"], 0.03)
        self.assertEqual(row["fault"]["chain"][0]["fault"]["counts"]["integration.work"], (1, 1))
        self.assertEqual(effort.counted()["integration.work"], (before[0] + 1, before[1] + 1))

    def test_actual_allocation_retry_keeps_both_failed_attempts_and_cap(self):
        before = effort.counted().get("integration.work", (0, 0))
        with self.workers() as workers, self.assertRaises(pool.TaskFailed) as caught:
            list(workers.map(allocation, [None]))
        self.assertEqual(caught.exception.key, "worker.memory")
        self.assertEqual(fault_evidence(caught.exception.fault)["configured_cap_bytes"], CAP)
        self.assertGreaterEqual(fault_evidence(caught.exception.fault)["cpu_seconds"], 0.03)
        self.assertEqual(effort.counted()["integration.work"], (before[0] + 2, before[1] + 2))

    def test_abrupt_child_exit_reports_observed_submission_wall(self):
        with self.workers() as workers:
            future = workers.submit(always, None)
            with self.assertRaises(Held):
                future.result()
            row = _result(future)
        self.assertEqual(row["key"], "worker.crash")
        self.assertGreaterEqual(row["seconds"], 0.03)
        self.assertEqual(row["fault"]["chain"][0]["fault"]["wall_scope"], "submission-to-completion")

    def test_submit_allocation_failure_retains_measured_worker_wall(self):
        with self.workers() as workers:
            future = workers.submit(allocation, None)
            with self.assertRaises(MemoryError):
                future.result()
            result = _result(future)
        self.assertEqual(result["key"], "worker.memory")
        self.assertGreaterEqual(result["seconds"], 0.03)

    def test_recovered_process_crash_is_visible_in_command_counts(self):
        before = effort.counted()
        with tempfile.TemporaryDirectory() as directory, self.workers() as workers:
            list(workers.map(once, [str(Path(directory) / "crash")]))
        after = effort.counted()
        for name in ("worker.crash", "worker.retry"):
            old = before.get(name, (0, 0))
            self.assertEqual(after.get(name), (old[0] + 1, old[1] + 1))
