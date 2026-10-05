"""Every command and step reports its CPU effort: this process, its tools and pool work by function."""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from unbake import effort


class EffortTests(unittest.TestCase):
    def test_since_counts_only_work_after_the_mark(self) -> None:
        usage = {"self": [1.0, 0.5], "children": [2.0, 0.0]}

        def rusage(who: int) -> SimpleNamespace:
            utime, stime = usage["self" if who == effort.resource.RUSAGE_SELF else "children"]
            return SimpleNamespace(ru_utime=utime, ru_stime=stime)

        with (
            patch.object(effort.resource, "getrusage", side_effect=rusage),
            patch.object(effort.time, "monotonic", side_effect=[10.0, 14.0]),
            patch.object(effort, "_ledger", {"typemap.facts._job": [5.0, 2]}),
        ):
            start = effort.mark()
            usage["self"], usage["children"] = [2.0, 0.5], [3.0, 0.0]
            effort.charge("typemap.facts._job", 7.0)
            effort.charge("work.plan._drafters", 1.0)
            spent = effort.since(start)
        self.assertEqual((spent.wall, spent.main, spent.tools), (4.0, 1.0, 1.0))
        self.assertEqual(spent.pool, {"typemap.facts._job": (7.0, 1), "work.plan._drafters": (1.0, 1)})
        self.assertEqual(
            spent.line(),
            "effort: 4.0 s wall, 10.0 cpu-s (250%): main 1.0, tools 1.0, pool 8.0 "
            "[typemap.facts._job 7.0 x1, work.plan._drafters 1.0 x1]",
        )
