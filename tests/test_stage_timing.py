"""Stages record their full path, own work, cores, pool runs and native calls, and a summary ranks them."""

import resource
import subprocess
import time
import unittest
from collections import OrderedDict
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from unbake import cache, effort, pool, process
from unbake.tui import progress


class Clock:
    """A fake monotonic clock and CPU source the test advances by hand."""

    def __init__(self) -> None:
        self.wall = 0.0
        self.cpu = 0.0

    def monotonic(self) -> float:
        return self.wall

    def rusage(self, who: int) -> SimpleNamespace:
        return SimpleNamespace(ru_utime=self.cpu if who == resource.RUSAGE_SELF else 0.0, ru_stime=0.0)

    def spend(self, wall: float, cpu: float) -> None:
        self.wall += wall
        self.cpu += cpu


class StageTimingTests(unittest.TestCase):
    def setUp(self) -> None:
        for name, value in (
            ("_ledger", {}),
            ("_counts", {}),
            ("_windows", [[0, 0]]),
            ("_stages", []),
            ("_pools", []),
            ("_run_rss", {}),
        ):
            patcher: Any = patch.object(effort, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.clock = Clock()
        for target, fake in (
            (time, ("monotonic", self.clock.monotonic)),
            (progress.time, ("monotonic", self.clock.monotonic)),
            (resource, ("getrusage", self.clock.rusage)),
            (effort, ("host_busy", lambda: 0.0)),
            (effort, ("resident_peak", lambda: 0)),
            (effort, ("_reset_peak", lambda: None)),
        ):
            patcher = patch.object(target, fake[0], fake[1])
            patcher.start()
            self.addCleanup(patcher.stop)

    def records(self) -> dict[str, effort.Stage]:
        return {stage.name: stage for stage in effort._stages}

    def test_nested_stages_record_paths_own_time_and_cores(self) -> None:
        with progress.task("Fold"):
            self.clock.spend(10.0, 10.0)
            with progress.task("Parse"):
                self.clock.spend(20.0, 20.0)
                with progress.task("Lex"):
                    self.clock.spend(5.0, 5.0)
            self.clock.spend(15.0, 15.0)
        rows = self.records()
        self.assertEqual(
            ["Fold > Parse > Lex", "Fold > Parse", "Fold > Parse (own)", "Fold", "Fold (own)"],
            [stage.name for stage in effort._stages],
        )
        self.assertEqual(5.0, rows["Fold > Parse > Lex"].wall)
        self.assertEqual(2, rows["Fold > Parse > Lex"].detail["depth"])
        self.assertEqual(20.0, rows["Fold > Parse (own)"].wall)
        self.assertEqual(25.0, rows["Fold (own)"].wall)
        self.assertEqual(25.0, rows["Fold (own)"].detail["parent_cpu_seconds"])
        self.assertEqual(1.0, rows["Fold (own)"].detail["cores"])
        self.assertTrue(rows["Fold (own)"].detail["own"])
        self.assertEqual(50.0, rows["Fold"].wall)

    def test_pool_runs_carry_items_workers_and_worker_cpu(self) -> None:
        with progress.task("Fold"):
            token = effort.pool_start("fold.job")
            self.clock.spend(10.0, 0.0)
            effort.charge("fold.job", 38.0, 300_000_000)
            effort.charge("fold.job", 2.0, 100_000_000)
            effort.pool_end(token, 80, 4)
        run = effort._pools[0]
        self.assertEqual(
            (run["pool"], run["items"], run["workers"], run["worker_cpu_seconds"], run["worker_rss_bytes"]),
            ("fold.job", 80, 4, 40.0, 300_000_000),
        )
        stage = self.records()["Fold"]
        self.assertEqual((stage.detail["items"], stage.detail["jobs"], stage.detail["workers"]), (80, 1, 4))
        self.assertEqual(40.0, stage.detail["worker_cpu_seconds"])
        self.assertEqual(4.0, stage.detail["cores"])

    def test_native_calls_are_aggregated_per_stage(self) -> None:
        with progress.task("Compile"):
            for _ in range(3):
                effort.native_call(2.0, 1.5)
            self.clock.spend(6.0, 0.0)
        detail = self.records()["Compile"].detail
        self.assertEqual(3, detail["native_calls"])
        self.assertAlmostEqual(6.0, float(detail["native_wall_seconds"]))
        self.assertAlmostEqual(4.5, float(detail["native_cpu_seconds"]))

    def test_run_native_counts_its_call(self) -> None:
        completed = SimpleNamespace(returncode=0, stdout="", stderr="")
        with patch.object(subprocess, "run", return_value=completed), progress.task("Assemble"):
            process.run_native(["as", "x.s"], Path("."), "assemble")
        self.assertEqual(1, self.records()["Assemble"].detail["native_calls"])

    def test_summary_ranks_by_wall_and_tags_serial_or_parallel(self) -> None:
        effort.record_stage("A", 5.0, 5.0)
        effort.record_stage("A > B", 50.0, 400.0)
        effort.record_stage("A (own)", 100.0, 100.0)
        effort.record_stage(f"{effort.PARENT_ONLY}A (own)", 100.0, 100.0)
        lines = effort.summary(effort._stages, top=2)
        self.assertEqual(2, len(lines))
        self.assertTrue(lines[0].rstrip().endswith("A (own)") and "serial" in lines[0])
        self.assertTrue(lines[1].rstrip().endswith("A > B") and "parallel" in lines[1])

    def test_parent_only_names_the_own_path(self) -> None:
        effort.expect_workers(4)
        self.addCleanup(effort.expect_workers, 1)
        with progress.task("Fold"):
            with progress.task("Pooled"):
                token = effort.pool_start("fold.job")
                self.clock.spend(5.0, 5.0)
                effort.charge("fold.job", 5.0)
                effort.pool_end(token, 10, 4)
            self.clock.spend(290.0, 290.0)
        spent = effort.since(effort.Mark(0.0, 0.0, 0.0, {}, 0, {}, 0.0, 0, 0))
        found = effort.step_findings(
            "fold",
            spent,
            SimpleNamespace(
                main_rss_bytes=1 << 60,
                worker_rss_bytes=1 << 60,
                step_cpu_seconds=1e9,
                main_cpu_seconds=1e9,
                main_cpu_fraction=1.0,
            ),
        )
        self.assertEqual(["budget.parent_only: Fold (own) ran in the parent process with workers > 1"], found)

    def test_memo_and_cache_hits_and_misses_are_counted_per_stage(self) -> None:
        keep = {"size": lambda value: 1, "copy_out": lambda value: value}
        with (
            patch.object(cache, "_budget", 1 << 20),
            patch.object(cache, "_memo", OrderedDict()),
            patch.object(cache, "_resident", 0),
            progress.task("Compare"),
        ):
            for _ in range(3):
                cache.memo("stage-timing.test", ("same",), lambda: 7, **keep)
        self.assertEqual({"memo.stage-timing.test": [2, 3]}, self.records()["Compare"].detail["cache"])

    def test_worker_memory_fault_names_action_limit_source_and_peak(self) -> None:
        def hungry() -> None:
            raise MemoryError

        with self.assertRaises(pool.WorkerMemory) as raised:
            pool._named(hungry)
        fault = dict(
            raised.exception.args[0],
            configured_cap_bytes=512,
            limit_source="resources.memory_worker_bytes in host.toml",
        )
        line = pool.memory_summary(fault)
        self.assertIn("test_stage_timing", fault["action"])
        self.assertIn("limit 512 bytes from resources.memory_worker_bytes in host.toml", line)
        self.assertIn(f"peaked at {fault['peak_rss_bytes'] or fault.get('vmdata_bytes')} bytes", line)

    def test_tree_nests_children_and_own_records(self) -> None:
        with progress.task("Fold"):
            with progress.task("Parse"):
                self.clock.spend(2.0, 2.0)
            self.clock.spend(1.0, 1.0)
        tree = effort.stage_tree(effort._stages)
        self.assertEqual(["Fold"], [node["name"] for node in tree])
        self.assertEqual(["Parse", "Fold (own)"], [child["name"] for child in tree[0]["children"]])


if __name__ == "__main__":
    unittest.main()
