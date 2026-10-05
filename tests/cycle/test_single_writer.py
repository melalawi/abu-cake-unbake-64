"""The cycle is the tree's one writer: steps and lands run only while no draft or compare is in flight."""

import hashlib
import io
import json
import queue
from concurrent.futures import Future
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tests.kit import TempCase
from unbake import land, pool, steps
from unbake.config import Held
from unbake.cycle import engine

QUEUE = queue.Queue


class Run(SimpleNamespace):
    def names(self, event: str, function: str | None = None) -> list[dict]:
        return [e for e in self.events if e["event"] == event and (function is None or e.get("function") == function)]


class SingleWriterTests(TempCase):
    def cycle(
        self,
        *,
        land_steps: list[object],
        edit_beta: bool = False,
        queue_beta_draft: bool = False,
    ) -> Run:
        """alpha is exact at its first compare; beta is 50% until alpha has landed.

        land_steps: what each ensure of LAND_STEPS does, in turn: the names that ran, a Held to raise, or "quit"."""
        project = SimpleNamespace(root=self.root, build=self.root / "build", work=self.root / "work", versions=("us",))
        host = SimpleNamespace(
            cycle_debounce_ms=100,
            workers=2,
            cores=2,
            memory_total_bytes=4,
            memory_parent_bytes=1,
            memory_worker_bytes=1,
            cache_root=self.root,
            publish_remote="",
            publish_branch="",
        )
        log: list[str] = []  # writes and task runs, in order
        pending: list[Future] = []  # tasks the pool has queued but not run
        drafts: dict[str, int] = {}
        compares: dict[str, int] = {}
        inboxes: list[queue.Queue] = []
        outcomes = list(land_steps)

        def draft_task(spec):
            _, _, function, replace = spec
            drafts[function] = drafts.get(function, 0) + 1
            log.append(f"draft {function}")
            file = project.work / function / f"{function}.c"
            file.parent.mkdir(parents=True, exist_ok=True)
            if file.is_file() and not replace:
                raise AssertionError(f"{function}: a draft replaced a file without replace")
            file.write_text(f"/* draft {drafts[function]} */\n")
            return {"ok": True, "file": str(file), "seconds": 0.0}

        def compare_task(spec):
            path = Path(spec[2])
            function = path.stem
            compares[function] = compares.get(function, 0) + 1
            log.append(f"compare {function}")
            exact = function == "alpha" or "land alpha" in log
            if function == "beta" and edit_beta and compares[function] == 1:
                inboxes[0].put(("edit", str(path)))  # saved by hand after this compare started
                path.write_text("/* edited by hand */\n")
                return {**result(exact=False), "sha256": hashlib.sha256(b"/* draft 1 */\n").hexdigest()}
            return {**result(exact=exact), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}

        def result(exact: bool) -> dict:
            return {
                "ok": True,
                "per_version": {},
                "best_percent": 100.0 if exact else 50.0,
                "exact": exact,
                "diagnostic": "",
                "seconds": 0.0,
            }

        held_once = {"beta"} if queue_beta_draft else set()

        class FakePool:
            size = 2

            @classmethod
            def from_host(cls, _host):
                return cls()

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                pass

            def submit(self, fn, spec):
                future: Future = Future()
                if fn is draft_task and spec[2] in held_once:
                    held_once.discard(spec[2])
                    pending.append(future)  # queued behind busy workers: never started
                    return future
                future.set_result(fn(spec))
                return future

        def busy() -> bool:
            return any(not future.done() for future in pending)

        def ensure(project_, host_, names, report=None):
            names = tuple(names)
            if busy():
                raise AssertionError(f"steps {names} ran while a task was in flight")
            log.append("ensure " + ",".join(names))
            if names != engine.LAND_STEPS:
                return []
            outcome = outcomes.pop(0) if outcomes else []
            if isinstance(outcome, Exception):
                raise outcome
            if outcome == "quit":  # nothing changed and beta waits for an edit: end the run here
                inboxes[0].put(("quit", None))
                return []
            ran = [steps.StepResult(name, "trigger", True, 1.0) for name in outcome]
            for done in ran:
                if report is not None:
                    report(done)
            return ran

        def fake_land(project_, host_, file):
            if busy():
                raise AssertionError("a land ran while a task was in flight")
            log.append(f"land {file.stem}")
            return "commit-" + file.stem

        def recheck(spec):
            log.append(f"recheck {spec[2]}")
            return {"exact": True, "best_percent": 100.0, "diagnostic": ""}

        def capture() -> queue.Queue:
            inbox: queue.Queue = QUEUE()
            inboxes.append(inbox)
            return inbox

        candidates = [
            SimpleNamespace(function=name, bytes=8, versions=("us",), carryover=False, best_percent=None)
            for name in ("alpha", "beta")
        ]
        stream = io.StringIO()
        with (
            patch.object(engine, "_draft_task", draft_task),
            patch.object(engine, "_compare_task", compare_task),
            patch.object(engine, "_recheck_task", recheck),
            patch.object(engine, "choose", lambda *a: candidates),
            patch.object(engine, "interactive", lambda: False),
            patch.object(engine.queue, "Queue", capture),
            patch.object(pool, "Pool", FakePool),
            patch.object(pool, "admitted", lambda *a: 2),
            patch.object(steps, "ensure", ensure),
            patch.object(land, "land", fake_land),
            patch.object(land, "subject", lambda *a: "Match"),
            patch.object(land, "record", lambda *a: None),
            patch.object(land, "dirty", lambda project: set()),
            patch.object(land, "commit_generated", lambda *a: None),
            patch("unbake.cycle.watcher.watch", lambda *a: None),
            patch("unbake.config.load", lambda root: project),
            patch("sys.stderr", io.StringIO()),
        ):
            outcome = engine.run(
                project,
                host,
                pick=2,
                functions=(),
                stop=None,
                push=False,
                events=stream,
                next_words=lambda *words: " ".join(words),
            )
        events = [json.loads(line) for line in stream.getvalue().splitlines()]
        return Run(events=events, result=outcome, log=log, drafts=drafts, compares=compares)

    def test_drafts_start_only_after_the_draft_steps(self) -> None:
        run = self.cycle(land_steps=["quit"])
        self.assertEqual(
            run.log[:2], ["ensure " + ",".join(engine.PICK_STEPS), "ensure " + ",".join(engine.DRAFT_STEPS)]
        )
        self.assertEqual(run.log[2], "draft alpha")

    def test_a_land_waits_for_every_task_in_flight(self) -> None:
        run = self.cycle(land_steps=["quit"])
        # beta's first compare was submitted before alpha was exact: it is read before alpha lands.
        self.assertLess(run.log.index("compare beta"), run.log.index("land alpha"))
        self.assertEqual(run.log[run.log.index("land alpha") + 1], "ensure " + ",".join(engine.LAND_STEPS))

    def test_steps_that_change_nothing_redo_nothing(self) -> None:
        run = self.cycle(land_steps=["quit"])
        self.assertEqual(run.drafts, {"alpha": 1, "beta": 1})
        self.assertEqual(run.result.data["landed"], ["alpha"])  # beta waits for an edit
        self.assertEqual(run.names("fn.recheck"), [])

    def test_an_untouched_draft_is_drafted_again_after_the_steps_change_the_tree(self) -> None:
        run = self.cycle(land_steps=[["types", "headers"], []])
        self.assertEqual(run.drafts, {"alpha": 1, "beta": 2})
        self.assertEqual(run.result.data["landed"], ["alpha", "beta"])
        self.assertEqual([e["function"] for e in run.names("fn.recheck")], ["alpha"])

    def test_an_edited_draft_is_compared_again_not_replaced(self) -> None:
        run = self.cycle(land_steps=[["types"], []], edit_beta=True)
        self.assertEqual(run.drafts, {"alpha": 1, "beta": 1})
        self.assertEqual(run.result.data["landed"], ["alpha", "beta"])
        self.assertEqual((self.root / "work" / "beta" / "beta.c").read_text(), "/* edited by hand */\n")

    def test_a_queued_task_is_cancelled_for_the_land_and_runs_after_the_steps(self) -> None:
        run = self.cycle(land_steps=[["types"], []], queue_beta_draft=True)
        land_at = run.log.index("land alpha")
        self.assertNotIn("draft beta", run.log[:land_at])
        self.assertEqual(run.drafts["beta"], 1)
        self.assertEqual(run.result.data["landed"], ["alpha", "beta"])

    def test_a_refused_step_stops_the_cycle_by_name(self) -> None:
        run = self.cycle(land_steps=[Held("solve", "types.declaration: broken")])
        self.assertEqual(run.result.key, "cycle.steps")
        self.assertEqual(run.names("steps.held")[0]["reason"], "types.declaration: broken")
        self.assertNotIn("draft beta", run.log[run.log.index("land alpha") :])
        self.assertEqual(run.result.data["landed"], ["alpha"])
