"""Shared real RageWars headers are indexed once; retained source checks never parse them again."""

import inspect
from collections import Counter
from pathlib import Path
from unittest.mock import patch

from tests.project_fixture import ProjectCase
from unbake import pool
from unbake.cache import forget
from unbake.config import Held
from unbake.layout import header_loss

FIXTURE = Path(__file__).parents[1] / "typemap/fixtures/ragewars_vec3/include/common/types_8a8189af7b05.h"


class RetentionWorkCounts(ProjectCase):
    versions = ("us",)

    def setUp(self):
        super().setUp()
        forget(["header-loss.header", "header-loss.source", "header-loss.names"])
        self.header = self.project.include[0] / "common/types_8a8189af7b05.h"
        self.header.parent.mkdir(parents=True)
        self.body = FIXTURE.read_text()
        self.header.write_text(self.body + "\nextern int UnusedRemoved;\n")
        for name in ("alpha", "beta", "gamma", "other"):
            (self.project.src / (name + ".c")).write_text(
                '#include "common/types_8a8189af7b05.h"\nvoid ' + name + "(Vec3 *v) { (void)v; }\n"
            )

    def check(self, outputs, counts=None):
        state = {"worker": False, "parent": 0, "headers": Counter(), "sources": 0}
        original = header_loss.declared

        def declared(text):
            state["parent"] += int(not state["worker"])
            state["headers"][text] += 1
            return original(text)

        def run(host, fn, jobs, shared=None):
            state["worker"] = True
            try:
                if fn.__name__ == "_source_job":
                    state["sources"] += sum(len(job) for job in jobs)
                return [fn(job) if shared is None else fn(shared, job) for job in jobs]
            finally:
                state["worker"] = False

        kwargs = {"policy": self.host} if "policy" in inspect.signature(header_loss.check).parameters else {}
        with patch.object(pool, "run", run), patch.object(header_loss, "declared", declared):
            header_loss.check(self.project, outputs, **kwargs)
        if counts is not None:
            counts.update(state)
        return state

    def test_real_header_is_parsed_once_for_many_consumers_with_zero_parent_items(self):
        before = self.header.read_text()
        counts = self.check({self.header: self.body.encode()})
        self.assertEqual(counts["parent"], 0)
        self.assertEqual(counts["headers"][before], 1)
        self.assertEqual(counts["headers"][self.body], 1)
        self.assertEqual(counts["sources"], 4)
        repeated = self.check({self.header: self.body.encode()})
        self.assertEqual(sum(repeated["headers"].values()), 0)

    def test_real_vec3_provider_loss_still_refuses_and_identifies_its_consumers(self):
        with self.assertRaisesRegex(Held, "alpha.c|beta.c|gamma.c"):
            self.check({self.header: b""})

    def test_a_moved_provider_must_still_be_reachable_by_each_consumer(self):
        moved = self.project.include[0] / "common/moved.h"
        with self.assertRaisesRegex(Held, "reachable declarations"):
            self.check({self.header: b"", moved: self.body.encode()})
        (self.project.src / "alpha.c").write_text('#include "common/moved.h"\nvoid alpha(Vec3 *v) { (void)v; }\n')
        outputs = {self.header: b"", moved: self.body.encode()}
        for source in self.project.src.glob("*.c"):
            outputs[source] = source.read_text().replace('"common/types_8a8189af7b05.h"', '"common/moved.h"').encode()
        self.check(outputs)
