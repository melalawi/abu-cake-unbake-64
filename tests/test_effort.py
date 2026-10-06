"""Every command and step reports its effort (this process, its tools, pool work by function, peak RSS, counts) and
is checked against the host's budgets."""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from tests.kit import BUDGETS
from unbake import effort

MB = effort.MB


class EffortTests(unittest.TestCase):
    def setUp(self) -> None:
        for name, value in (("_ledger", {}), ("_counts", {}), ("_windows", [[0, 0]])):
            patcher = patch.object(effort, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.peak = 10 * MB
        for name, fake in (("resident_peak", lambda: self.peak), ("_reset_peak", lambda: None)):
            patcher = patch.object(effort, name, fake)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_since_counts_only_work_after_the_mark(self) -> None:
        usage = {"self": [1.0, 0.5], "children": [2.0, 0.0]}

        def rusage(who: int) -> SimpleNamespace:
            utime, stime = usage["self" if who == effort.resource.RUSAGE_SELF else "children"]
            return SimpleNamespace(ru_utime=utime, ru_stime=stime)

        effort.charge("typemap.facts._job", 5.0)
        effort.count("facts", 3, 10)
        with (
            patch.object(effort.resource, "getrusage", side_effect=rusage),
            patch.object(effort.time, "monotonic", side_effect=[10.0, 14.0]),
        ):
            start = effort.mark()
            usage["self"], usage["children"] = [2.0, 0.5], [3.0, 0.0]
            effort.charge("typemap.facts._job", 7.0, rss=300 * MB)
            effort.charge("work.plan._drafters", 1.0)
            effort.count("facts", 1, 100)
            spent = effort.since(start)
        self.assertEqual((spent.wall, spent.main, spent.tools), (4.0, 1.0, 1.0))
        self.assertEqual(spent.pool, {"typemap.facts._job": (7.0, 1), "work.plan._drafters": (1.0, 1)})
        self.assertEqual(spent.counts, {"facts": (1, 100)})
        document = spent.document()
        self.assertEqual(
            {key: document[key] for key in ("cpu_percent", "pool_cpu_seconds", "worker_rss_bytes", "counts")},
            {
                "cpu_percent": 250.0,
                "pool_cpu_seconds": 8.0,
                "worker_rss_bytes": 300 * MB,
                "counts": {"facts": [1, 100]},
            },
        )

    def test_a_window_holds_its_own_peak_and_a_wider_mark_sees_the_highest(self) -> None:
        outer = effort.mark()
        effort.window()
        first = effort.mark()
        self.peak = 900 * MB
        effort.charge("a", 1.0, rss=50 * MB)
        self.assertEqual((effort.since(first).main_rss, effort.since(first).worker_rss), (900 * MB, 50 * MB))
        effort.window()
        self.peak = 20 * MB
        second = effort.mark()
        self.assertEqual((effort.since(second).main_rss, effort.since(second).worker_rss), (20 * MB, 0))
        self.assertEqual((effort.since(outer).main_rss, effort.since(outer).worker_rss), (900 * MB, 50 * MB))


def budgets(**changes: float) -> SimpleNamespace:
    return SimpleNamespace(**{**BUDGETS, **changes})


def spent(
    wall: float = 1.0,
    main_rss: int = 0,
    worker_rss: int = 0,
    facts: tuple[int, int] | None = None,
    main: float = 0.0,
    pool: float = 0.0,
    external: float = 0.0,
):
    return effort.Effort(
        wall,
        main,
        0.0,
        {"f": (pool, 1)} if pool else {},
        main_rss,
        worker_rss,
        {"facts": facts} if facts else {},
        external,
    )


class FindingTests(unittest.TestCase):
    def test_step_memory_at_the_budget_passes_and_above_is_named(self) -> None:
        limits = budgets(main_rss_bytes=100 * MB, worker_rss_bytes=10 * MB)
        for label, used, expected in [
            ("at both budgets", spent(main_rss=100 * MB, worker_rss=10 * MB), []),
            ("main over", spent(main_rss=100 * MB + 1), ["budget.main_rss_bytes"]),
            ("worker over", spent(worker_rss=11 * MB), ["budget.worker_rss_bytes"]),
        ]:
            with self.subTest(label):
                found = effort.step_findings("types", used, limits)
                self.assertEqual([line.split(":")[0] for line in found], expected)
                self.assertTrue(all("types peaked at" in line for line in found))

    def test_chain_seconds_and_facts_misses_by_kind(self) -> None:
        limits = budgets(recompute_seconds=450, unchanged_seconds=30, changed_seconds=90, facts_miss_fraction=0.01)
        for label, kind, used, expected in [
            ("changed at its budget", "changed", spent(90.0, facts=(1, 100)), []),
            ("changed over", "changed", spent(90.5), ["budget.changed_seconds"]),
            ("unchanged over", "unchanged", spent(31.0), ["budget.unchanged_seconds"]),
            ("recompute misses never count", "recompute", spent(10.0, facts=(100, 100)), []),
            ("changed misses over", "changed", spent(10.0, facts=(2, 100)), ["budget.facts_miss_fraction"]),
            ("no solve ran", "changed", spent(10.0), []),
        ]:
            with self.subTest(label):
                found, notes = effort.chain_findings(kind, used, limits)
                self.assertEqual([line.split(":")[0] for line in found], expected)
                self.assertEqual(notes, [])

    def test_cpu_is_the_verdict_and_a_contended_wall_miss_is_only_noted(self) -> None:
        limits = budgets(recompute_seconds=450, recompute_cpu_seconds=3000, contended_cores=2)
        for label, used, findings, contended in [
            ("wall miss on a quiet host", spent(500.0, pool=2000.0, external=100.0), ["budget.recompute_seconds"], 0),
            (
                "near miss: 1.9 other cores is not contended",
                spent(500.0, external=950.0),
                ["budget.recompute_seconds"],
                0,
            ),
            ("wall miss under 2 other cores", spent(500.0, pool=2000.0, external=1000.0), [], 1),
            (
                "cpu over is a failure whatever the load",
                spent(400.0, pool=3001.0, external=4000.0),
                ["budget.recompute_cpu_seconds"],
                0,
            ),
        ]:
            with self.subTest(label):
                found, notes = effort.chain_findings("recompute", used, limits)
                self.assertEqual([line.split(":")[0] for line in found], findings)
                self.assertEqual(len(notes), contended)
                self.assertTrue(all(note.startswith("contended budget.recompute_seconds") for note in notes))

    def test_a_single_core_stretch_is_its_own_finding(self) -> None:
        limits = budgets(main_cpu_seconds=60, main_cpu_fraction=0.5, step_cpu_seconds=1000)
        for label, used, expected in [
            ("small step mostly in one process", spent(main=50.0, pool=1.0), []),
            ("long step spread over workers", spent(main=100.0, pool=400.0), []),
            ("long step mostly in one process", spent(main=300.0, pool=100.0), ["budget.main_cpu_fraction"]),
            ("step cpu over", spent(main=10.0, pool=1000.0), ["budget.step_cpu_seconds"]),
        ]:
            with self.subTest(label):
                found = effort.step_findings("types", used, limits)
                self.assertEqual([line.split(":")[0] for line in found], expected)


class HostLoadTests(unittest.TestCase):
    def test_other_processes_cpu_is_host_busy_time_less_ours(self) -> None:
        busy = iter([1000.0, 1300.0])
        with (
            patch.object(effort, "host_busy", lambda: next(busy)),
            patch.object(effort, "_ledger", {}),
            patch.object(effort, "_counts", {}),
            patch.object(effort, "_windows", [[0, 0]]),
            patch.object(effort, "resident_peak", lambda: 0),
            patch.object(effort.time, "monotonic", side_effect=[0.0, 100.0]),
        ):
            start = effort.mark()
            effort.charge("f", 200.0)
            used = effort.since(start)
        self.assertEqual((round(used.external, 2), round(used.external_cores, 2)), (100.0, 1.0))
        self.assertEqual(used.document()["external_cores"], 1.0)

    def test_proc_stat_busy_excludes_idle_and_iowait(self) -> None:
        stat = "cpu  100 20 30 5000 70 8 2 0 0 0\ncpu0 1 2 3\n"
        with (
            patch.object(effort.Path, "read_text", return_value=stat),
            patch.object(effort.os, "sysconf", return_value=100),
        ):
            self.assertEqual(effort.host_busy(), 1.6)


class ExactCountTests(unittest.TestCase):
    def test_bool_negative_or_out_of_total_counts_fail_before_accounting(self):
        for done, total in ((True, 1), (-1, 1), (2, 1), (0, -1)):
            with self.assertRaises(ValueError):
                effort.count("invalid", done, total)
        self.assertNotIn("invalid", effort.counted())
