"""Real C/MIPS inputs count parent work, cache misses and touched type components."""

import inspect
from dataclasses import replace
from unittest.mock import patch

from tests.project_fixture import ProjectCase
from tests.typemap.test_solver import facts
from unbake import pool
from unbake.typemap import closure, evidence, header_names, regeneration


class HeaderConsumerCounts(ProjectCase):
    versions = ("us",)

    def test_a_landing_or_unit_flag_change_projects_only_its_consumer_in_a_worker(self):
        paths = [self.project.src / (name + ".c") for name in ("alpha", "beta")]
        for path in paths:
            path.write_text("int " + path.stem + "(void) { return 1; }\n")
        state = {"worker": False, "serial": 0, "sources": []}

        def owned(job):
            state["serial"] += int(not state["worker"])
            state["sources"].append(job[2].stem)
            return [], []

        def run(host, fn, jobs, shared=None):
            state["worker"] = True
            try:
                return [fn(job) if shared is None else fn(shared, job) for job in jobs]
            finally:
                state["worker"] = False

        with patch.object(pool, "run", run), patch.object(header_names, "_owned", owned):
            regeneration.Session(self.project, self.host)
            self.assertEqual(state["serial"], 0)
            self.assertEqual(state["sources"], ["alpha", "beta"])
            state["sources"].clear()
            regeneration.Session(self.project, self.host)
            self.assertEqual(state["sources"], [])
            paths[0].write_text(paths[0].read_text() + "/* landed */\n")
            regeneration.Session(self.project, self.host)
            self.assertEqual(state["sources"], ["alpha"])
            state["sources"].clear()
            changed = replace(self.project, unit_flags={"beta": ("-DBETA_ONLY=1",)})
            regeneration.Session(changed, self.host)
            self.assertEqual(state["sources"], ["beta"])
            self.assertEqual(state["serial"], 0)


class ComponentCounts(ProjectCase):
    versions = ("us",)

    def test_full_and_touched_component_resolutions_have_zero_parent_items(self):
        mapped = facts({"leaf": (0x80001000, [0x24020001, 0x03E00008, 0])})
        state = {"worker": False, "serial": 0, "items": 0}
        original = closure.resolve

        def resolve(*args):
            state["serial"] += int(not state["worker"])
            state["items"] += 1
            return original(*args)

        def run(host, fn, jobs, shared=None):
            state["worker"] = True
            try:
                return [fn(job) if shared is None else fn(shared, job) for job in jobs]
            finally:
                state["worker"] = False

        with patch.object(pool, "run", run), patch.object(closure, "resolve", resolve):
            abi = evidence.abi(mapped["functions"], {}, self.host)
            machine = closure.build(mapped["functions"], abi, {}, {"us": {}}, None, self.host)
            self.assertEqual(state["serial"], 0)
            self.assertEqual(state["items"], len(machine.groups))
            declared = closure.Constraints()
            declared.seed("result:leaf:r2", "int", {"kind": "proven", "source": "leaf.c"})
            graph = closure.Closure(machine, declared)
            state["items"] = 0
            if "host" in inspect.signature(graph.close).parameters:
                graph.close(self.host)
            else:
                graph.close()
            self.assertEqual(state["serial"], 0)
            self.assertEqual(state["items"], 1)
            state["items"] = 0
            if "host" in inspect.signature(graph.close).parameters:
                graph.close(self.host)
            else:
                graph.close()
            self.assertEqual(state["items"], 0)
