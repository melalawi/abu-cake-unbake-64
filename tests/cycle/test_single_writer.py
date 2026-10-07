"""The cycle is the tree's one writer: steps and lands run only while no draft or compare is in flight."""

import hashlib
import io
import json
import queue
from collections.abc import Callable
from concurrent.futures import Future
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tests.kit import TempCase
from unbake import land, pool, steps
from unbake.config import Held
from unbake.cycle import engine, ladder
from unbake.inputs import DependencySet
from unbake.process import named
from unbake.work import attempts
from unbake.work.score import measure_words

QUEUE = queue.Queue


class Run(SimpleNamespace):
    def names(self, event: str, function: str | None = None) -> list[dict]:
        return [e for e in self.events if e["event"] == event and (function is None or e.get("function") == function)]


class SingleWriterTests(TempCase):
    def setUp(self) -> None:
        super().setUp()
        # These cases are about the ladder's mechanics, not which methods the tool ships.
        patcher = patch.object(ladder, "BUILTINS", ("registers", "order", "permute"))
        patcher.start()
        self.addCleanup(patcher.stop)

    def cycle(
        self,
        *,
        land_steps: list[object],
        edit_beta: bool = False,
        compare_fault: bool = False,
        queue_beta_draft: bool = False,
        interrupt_land: bool = False,
        search: Callable[[int, str, str], str | dict | None] | None = None,
    ) -> Run:
        """alpha is exact at its first compare; beta is 50% until alpha has landed.

        land_steps: what each ensure of LAND_STEPS does, in turn: the names that ran, a Held to raise, or "quit".
        search: (call number, the file's text, method) -> the text of the method's best file, a result dict, or
        None for the file as it is, "SKIP" for a method with no mutation to propose.
        A beta text spelling "better" scores 10 points more each time, "worse" 10 less, "EXACT" is exact.
        The run ends when beta needs a creative edit."""
        project = SimpleNamespace(
            root=self.root,
            id="fixture",
            build=self.root / "build",
            work=self.root / "work",
            cache=self.root,
            versions=("us",),
        )
        host = SimpleNamespace(
            cycle_debounce_ms=100,
            cycle_search_seconds=1,
            workers=2,
            cores=2,
            memory_total_bytes=4,
            memory_parent_bytes=1,
            memory_worker_bytes=1,
        )
        log: list[str] = []  # writes and task runs, in order
        published: dict[str, str] = {}
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
            text = path.read_text()
            if function == "beta" and compare_fault:
                return {"ok": False, "key": "compare", "diagnostic": "measurement unavailable", "seconds": 0.0}
            exact = function == "alpha" or ("land alpha" in log and search is None) or "EXACT" in text
            if function == "beta" and edit_beta and compares[function] == 1:
                inboxes[0].put(("edit", str(path)))  # saved by hand after this compare started
                path.write_text("/* edited by hand */\n")
                return {**result(exact=False), "sha256": hashlib.sha256(b"/* draft 1 */\n").hexdigest()}
            percent = 100.0 if exact else 50.0 + 10.0 * text.count("better") - 10.0 * text.count("worse")
            return {**result(exact=exact, percent=percent), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}

        def result(exact: bool, percent: float | None = None) -> dict:
            return {
                "ok": True,
                "per_version": {},
                "best_percent": percent if percent is not None else 100.0 if exact else 50.0,
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

            def _fresh(self):
                log.append("fresh")

            def submit(self, fn, spec):
                future: Future = Future()
                if fn is draft_task and spec[2] in held_once:
                    held_once.discard(spec[2])
                    pending.append(future)  # queued behind busy workers: never started
                    return future
                if fn is engine.execute_task:
                    operation, produce, arguments = spec
                    if produce is draft_task and arguments[2] in held_once:
                        held_once.discard(arguments[2])
                        pending.append(future)
                        return future
                    value = produce(arguments)
                    fault = (
                        None
                        if value["ok"]
                        else __import__("unbake.process", fromlist=["Fault"]).Fault(
                            named(
                                value.get("key", "fixture.refused"),
                                value["diagnostic"],
                                owner="fixture",
                                stage="cycle",
                                dependencies=operation.dependencies,
                            )
                        )
                    )
                    future.set_result(
                        attempts.Outcome(operation.id, "ok" if value["ok"] else "blocked", value, fault, {})
                    )
                else:
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

        def fake_land(project_, host_, file, *, fuzzy=False, on_commit=None):
            if busy():
                raise AssertionError("a land ran while a task was in flight")
            log.append(f"{'fuzzy' if fuzzy else 'land'} {file.stem}")
            if interrupt_land:
                raise KeyboardInterrupt
            published[file.stem] = file.read_text()
            if on_commit is not None:
                on_commit(
                    {
                        "function": file.stem,
                        "commit": "commit-" + file.stem,
                        "message": "Fuzzy " + file.stem,
                        "proof": {"kind": "fuzzy", "score": 50.0},
                    }
                )
            return "commit-" + file.stem

        def recheck(spec):
            log.append(f"recheck {spec[2]}")
            return {"exact": True, "best_percent": 100.0, "diagnostic": ""}

        searches: list[str] = []

        def search_task(spec):
            _, _, file, method = spec
            path = Path(file)
            searches.append(method)
            log.append(f"search {path.stem} {method}")
            found = search(len(searches), path.read_text(), method) if search is not None else None
            if isinstance(found, dict):
                return found
            if found == "SKIP":
                return {
                    "ok": True,
                    "best_file": str(path),
                    "mutations": 0,
                    "measurements": {
                        "us": measure_words(
                            "us", bytes.fromhex("24420004") * 4, bytes.fromhex("24420004") + bytes(12)
                        ).document()
                    },
                    "seconds": 0.0,
                }
            best = path.with_name(f"{path.stem}.best.c")
            best.write_text(path.read_text() if found is None else found)
            return {
                "ok": True,
                "best_file": str(best),
                "mutations": 1,
                "measurements": {
                    "us": measure_words(
                        "us", bytes.fromhex("24420004") * 4, bytes.fromhex("24420004") + bytes(12)
                    ).document()
                },
                "seconds": 0.0,
            }

        def write_trouble(project_, host_, function, file, ladder_, difference):
            log.append(f"trouble {function}")
            inboxes[0].put(("quit", None))
            return file.with_name("TROUBLE.md")

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
            patch.object(
                engine, "task_dependencies", return_value=DependencySet((), {"dependencies_unknown": True}, {})
            ),
            patch.object(engine, "_draft_task", draft_task),
            patch.object(engine, "_compare_task", compare_task),
            patch.object(engine, "_search_task", search_task),
            patch.object(ladder, "write_trouble", write_trouble),
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
                events=stream,
                next_words=lambda *words: " ".join(words),
            )
        events = [json.loads(line) for line in stream.getvalue().splitlines()]
        return Run(
            events=events,
            result=outcome,
            log=log,
            drafts=drafts,
            compares=compares,
            searches=searches,
            published=published,
        )

    def test_drafts_start_only_after_the_draft_steps(self) -> None:
        run = self.cycle(land_steps=["quit"])
        self.assertEqual(
            run.log[:2], ["ensure " + ",".join(engine.PICK_STEPS), "ensure " + ",".join(engine.DRAFT_STEPS)]
        )
        self.assertEqual(run.log[2], "draft alpha")

    def test_interrupted_land_leaves_the_exact_row_ready_without_a_final_cause(self) -> None:
        run = self.cycle(land_steps=[], interrupt_land=True)
        self.assertEqual((run.result.status, run.result.key), ("interrupted", None))
        self.assertTrue(run.result.data["retryable"])
        self.assertEqual(run.log.count("land alpha"), 1)
        self.assertEqual(run.published, {})
        self.assertEqual(run.names("fn.land_failed"), [])
        self.assertEqual(run.names("fn.held"), [])
        state = json.loads((self.root / "build/cycle/state.json").read_text())
        alpha = next(row for row in state["rows"] if row["function"] == "alpha")
        self.assertEqual((alpha["stage"], alpha["held"]), ("ready", False))

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
        self.assertNotIn("fresh", run.log)  # no step ran, so no worker holds anything new

    def test_an_untouched_draft_is_drafted_again_after_the_steps_change_the_tree(self) -> None:
        run = self.cycle(land_steps=[["types", "headers"], []])
        self.assertEqual(run.drafts, {"alpha": 1, "beta": 2})
        self.assertEqual(run.result.data["landed"], ["alpha", "beta"])
        self.assertEqual([e["function"] for e in run.names("fn.recheck")], ["alpha"])
        # The steps' workers are replaced after the land's steps and before the redone draft.
        redo = len(run.log) - 1 - run.log[::-1].index("draft beta")
        self.assertLess(run.log.index("ensure " + ",".join(engine.LAND_STEPS)), run.log.index("fresh"))
        self.assertLess(run.log.index("fresh"), redo)

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
        run = self.cycle(
            land_steps=[Held(named("types.declaration", "types.declaration: broken", owner="fixture", stage="solve"))]
        )
        self.assertEqual(run.result.key, "cycle.steps")
        self.assertEqual(run.names("steps.held")[0]["reason"], "types.declaration: broken")
        self.assertNotIn("draft beta", run.log[run.log.index("land alpha") :])
        self.assertEqual(run.result.data["landed"], ["alpha"])

    # A draft that compiles but is short climbs search.BUILTINS in order before anyone is asked to edit it.

    def methods(self, run: Run) -> list[str]:
        return [e["method"] for e in run.names("fn.search.start", "beta")]

    def test_a_method_that_gains_nothing_ends_the_ladder_at_once(self) -> None:
        run = self.cycle(land_steps=[[]], search=lambda n, text, method: None)
        self.assertEqual(self.methods(run), ["registers"])
        creative = run.names("fn.creative", "beta")[0]
        self.assertEqual(creative["methods"], {"registers": 50.0})
        self.assertEqual(creative["best_percent"], 50.0)
        self.assertEqual(run.result.data["carryovers"], ["beta"])

    def test_each_gaining_method_hands_its_best_text_to_the_next(self) -> None:
        run = self.cycle(land_steps=[[]], search=lambda n, text, method: text + "better\n")
        self.assertEqual(self.methods(run), ["registers", "order", "permute"])
        creative = run.names("fn.creative", "beta")[0]
        self.assertEqual(creative["methods"], {"registers": 60.0, "order": 70.0, "permute": 80.0})
        self.assertEqual((self.root / "work" / "beta" / "beta.c").read_text().count("better"), 3)

    def test_a_middle_method_without_gain_stops_before_the_rest(self) -> None:
        run = self.cycle(land_steps=[[]], search=lambda n, text, method: text + "better\n" if n == 1 else None)
        self.assertEqual(self.methods(run), ["registers", "order"])
        self.assertEqual(run.names("fn.creative", "beta")[0]["methods"], {"registers": 60.0, "order": 60.0})

    def test_a_worse_result_leaves_the_best_text_in_the_file(self) -> None:
        run = self.cycle(land_steps=[[]], search=lambda n, text, method: text + ("better\n" if n == 1 else "worse\n"))
        text = (self.root / "work" / "beta" / "beta.c").read_text()
        self.assertEqual((text.count("better"), text.count("worse")), (1, 0))
        creative = run.names("fn.creative", "beta")[0]
        self.assertEqual(creative["methods"], {"registers": 60.0, "order": 50.0})
        self.assertEqual(creative["best_percent"], 60.0)

    def test_an_exact_search_result_lands_like_any_exact_compare(self) -> None:
        run = self.cycle(land_steps=[[], []], search=lambda n, text, method: text + "EXACT\n")
        self.assertEqual(run.result.data["landed"], ["alpha", "beta"])
        self.assertEqual(run.names("fn.creative"), [])
        self.assertEqual(self.methods(run), ["registers"])

    def test_a_method_that_errors_holds_the_row_with_its_reason(self) -> None:
        def failing(n: int, text: str, method: str) -> dict:
            return {"ok": False, "key": "search", "diagnostic": f"{method} broke", "seconds": 0.0}

        run = self.cycle(land_steps=[[]], search=failing)
        self.assertEqual(self.methods(run), ["registers"])  # no method after the one that errored
        self.assertEqual(run.names("fn.creative"), [])
        held = run.names("fn.held", "beta")[0]
        self.assertEqual(held["reason"], "search.registers: registers broke")
        self.assertEqual(run.result.data["held"], ["beta"])
        self.assertEqual(run.result.data["fuzzy"], ["beta"])
        self.assertIn("draft 1", run.published["beta"])

    def test_a_method_with_nothing_to_mutate_is_skipped_and_the_next_one_runs(self) -> None:
        run = self.cycle(land_steps=[[]], search=lambda n, text, method: "SKIP" if n == 1 else None)
        self.assertEqual(self.methods(run), ["registers", "order"])
        skipped = [e for e in run.names("fn.search.done", "beta") if e.get("diagnostic")]
        self.assertEqual([e["method"] for e in skipped], ["registers"])
        creative = run.names("fn.creative", "beta")[0]
        self.assertEqual(creative["methods"]["order"], 50.0)
        self.assertTrue(creative["methods"]["registers"].startswith("skipped:"))
        self.assertEqual(run.names("fn.held"), [])

    def test_every_method_skipped_ends_in_a_creative_edit_not_a_hold(self) -> None:
        run = self.cycle(land_steps=[[]], search=lambda n, text, method: "SKIP")
        self.assertEqual(self.methods(run), ["registers", "order", "permute"])
        self.assertEqual(set(run.names("fn.creative", "beta")[0]["methods"]), {"registers", "order", "permute"})
        self.assertEqual(run.names("fn.held"), [])

    def test_a_search_started_before_the_land_runs_after_the_steps(self) -> None:
        run = self.cycle(land_steps=[[]], search=lambda n, text, method: None)
        self.assertLess(run.log.index("land alpha"), run.log.index("search beta registers"))

    def test_best_source_is_published_fuzzy_at_the_drained_boundary(self) -> None:
        run = self.cycle(land_steps=[[]], search=lambda n, text, method: text + ("better\n" if n == 1 else "worse\n"))
        self.assertEqual(run.result.data["fuzzy"], ["beta"])
        self.assertEqual(run.result.data["landed"], ["alpha"])
        self.assertEqual(run.names("cycle.end")[0]["landed_bytes"], 8)
        self.assertEqual(run.published["beta"], (self.root / "work/beta/beta.ladder.c").read_text())
        self.assertIn("better", run.published["beta"])
        self.assertNotIn("worse", run.published["beta"])
        self.assertEqual(run.names("fn.fuzzy_landed")[0]["commit"], "commit-beta")
        self.assertEqual(run.names("fn.committed", "beta")[0]["proof"]["kind"], "fuzzy")
        self.assertFalse(any(line.startswith("ensure ") for line in run.log[run.log.index("fuzzy beta") + 1 :]))

    def test_unavailable_comparison_still_offers_source_to_fuzzy_admission(self) -> None:
        run = self.cycle(land_steps=[[]], compare_fault=True)
        self.assertEqual(run.result.data["fuzzy"], ["beta"])
        self.assertEqual(run.result.data["landed"], ["alpha"])
        self.assertEqual(run.searches, [])
        self.assertEqual(run.names("fn.compare.done", "beta")[0]["best_percent"], None)
        self.assertEqual(run.names("cycle.end")[0]["landed_bytes"], 8)
