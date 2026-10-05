"""The cycle drafts at once while types, headers and build files refresh behind it."""

import fcntl
import hashlib
import io
import json
import os
import queue
import threading
from concurrent.futures import Future
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tests.kit import TempCase
from unbake import land, lock, pool, steps
from unbake.config import Held
from unbake.cycle import engine
from unbake.cycle import refresh as background


class RefreshThreadTests(TempCase):
    def run_refresh(self, outcomes: list) -> tuple[list[str], list[tuple[str, object]], int]:
        inbox: queue.Queue = queue.Queue()
        calls: list[str] = []
        refresh = background.Refresh(self.root, SimpleNamespace(), inbox)
        gate = threading.Event()

        def ensure(project, host, names, report):
            calls.append(",".join(names))
            outcome = outcomes[len(calls) - 1]
            if len(calls) == 1:
                refresh.request()  # a land while the first run is in progress
                gate.set()
            if isinstance(outcome, Exception):
                raise outcome
            done = steps.StepResult(outcome, "trigger", True, 1.0)
            report(done)
            return [done]

        with patch.object(steps, "ensure", ensure), patch("unbake.config.load", lambda root: None):
            refresh.request()
            gate.wait()
            refresh.join()
        events = []
        while not inbox.empty():
            events.append(inbox.get())
        return calls, events, len(calls)

    def test_a_request_during_a_run_runs_once_more_and_reports_once(self) -> None:
        calls, events, _ = self.run_refresh(["types", "headers"])
        self.assertEqual(calls, ["types,headers,buildfiles"] * 2)
        self.assertEqual([kind for kind, _ in events], ["step", "step", "refreshed"])
        self.assertEqual(events[-1][1].steps, ("types", "headers"))
        self.assertEqual(events[-1][1].diagnostic, "")

    def test_a_failed_step_is_reported_and_ends_the_run(self) -> None:
        calls, events, _ = self.run_refresh([Held("solve", "types.declaration: broken"), "headers"])
        self.assertEqual(len(calls), 1)
        self.assertEqual([kind for kind, _ in events], ["refreshed"])
        self.assertEqual(events[-1][1].diagnostic, "types.declaration: broken")


class PublishSectionTests(TempCase):
    def probe(self, name: str) -> bool:
        """True when a shared lock on build/NAME could be taken now."""
        descriptor = os.open(self.root / "build" / name, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        finally:
            os.close(descriptor)
        return True

    def test_readers_never_enter_while_a_set_is_half_published(self) -> None:
        include = self.root / "include"
        include.mkdir()
        seen: list[set[str]] = []
        with lock.publishing(self.root):
            (include / "a.h").write_text("2")
            self.assertFalse(self.probe("publish.gate"))
            self.assertFalse(self.probe("publish.lock"))

            def read() -> None:
                with lock.reading(self.root):
                    seen.append({path.read_text() for path in include.iterdir()})

            reader = threading.Thread(target=read)
            reader.start()
            with lock.publishing(self.root):  # a land's build files: the same thread enters again
                (include / "b.h").write_text("2")
        reader.join()
        self.assertEqual(seen, [{"2"}])
        self.assertTrue(self.probe("publish.gate") and self.probe("publish.lock"))


class FakePool:
    size = 2

    @classmethod
    def from_host(cls, host: object) -> "FakePool":
        return cls()

    def __enter__(self) -> "FakePool":
        return self

    def __exit__(self, *exc: object) -> None:
        pass

    def submit(self, fn, spec):
        future: Future = Future()
        future.set_result(fn(spec))
        return future

    def map(self, fn, items):
        return [fn(item) for item in items]


class CycleStartTests(TempCase):
    def run_cycle(self, recheck_exact: bool) -> SimpleNamespace:
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
        drafts: dict[str, int] = {}
        compares: dict[str, int] = {}
        requests: list[int] = []
        ensured: list[tuple[str, ...]] = []
        landed_inside_publish: list[bool] = []
        inboxes: list[queue.Queue] = []

        def sha(path: Path) -> str:
            return hashlib.sha256(path.read_bytes()).hexdigest()

        def draft_task(spec):
            _, _, function, _ = spec
            drafts[function] = drafts.get(function, 0) + 1
            file = project.work / function / f"{function}.c"
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text(f"/* draft {drafts[function]} */\n")
            return {"ok": True, "file": str(file), "seconds": 0.0}

        def compare_task(spec):
            _, _, file = spec
            path = Path(file)
            function = path.stem
            compares[function] = compares.get(function, 0) + 1
            submitted = sha(path)
            if function == "beta" and compares[function] == 1:
                path.write_text("/* edited by hand */\n")
                inboxes[0].put(("refreshed", background.Refreshed(("types", "headers"), 1.0)))
            exact = compares[function] > 1
            return {
                "ok": True,
                "sha256": submitted,
                "per_version": {},
                "best_percent": 100.0 if exact else 50.0,
                "exact": exact,
                "diagnostic": "",
                "seconds": 0.0,
            }

        class FakeRefresh:
            def __init__(self, root, host, inbox):
                inboxes.append(inbox)

            def request(self) -> None:
                requests.append(1)
                if not recheck_exact and len(requests) == 2:  # the first land's refresh replaces headers at once
                    inboxes[0].put(("refreshed", background.Refreshed(("headers",), 1.0)))

            def join(self) -> None:
                pass

        def fake_land(project_, host_, file):
            landed_inside_publish.append(bool(lock._publishing.__dict__.get("held", {}).get(self.root)))
            return "commit"

        def ensure(project_, host_, names, report=None):
            ensured.append(tuple(names))
            return []

        rechecked: list[str] = []

        def recheck(spec):
            rechecked.append(spec[2])
            return {
                "exact": recheck_exact,
                "best_percent": 100.0 if recheck_exact else 75.0,
                "diagnostic": "" if recheck_exact else "first divergence: 0x10",
            }

        candidates = [
            SimpleNamespace(function=name, bytes=8, versions=("us",), carryover=False, best_percent=None)
            for name in ("alpha", "beta")
        ]
        stream = io.StringIO()
        with (
            patch.object(engine, "_draft_task", draft_task),
            patch.object(engine, "_compare_task", compare_task),
            patch.object(engine, "choose", lambda *a: candidates),
            patch.object(engine, "interactive", lambda: False),
            patch.object(pool, "Pool", FakePool),
            patch.object(pool, "admitted", lambda *a: 2),
            patch.object(background, "Refresh", FakeRefresh),
            patch.object(steps, "ensure", ensure),
            patch.object(land, "land", fake_land),
            patch.object(land, "subject", lambda *a: "Match"),
            patch.object(land, "record", lambda *a: None),
            patch.object(land, "dirty", lambda project: set()),
            patch.object(land, "commit_generated", lambda *a: None),
            patch.object(engine, "_recheck_task", recheck),
            patch("unbake.cycle.watcher.watch", lambda *a: None),
            patch("unbake.config.load", lambda root: project),
            patch("sys.stderr", io.StringIO()),
        ):
            result = engine.run(
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
        return SimpleNamespace(
            events=events,
            result=result,
            drafts=drafts,
            compares=compares,
            ensured=ensured,
            landed_inside_publish=landed_inside_publish,
            requests=len(requests),
        )

    def test_drafts_start_at_once_and_the_refresh_redrafts_only_untouched_drafts(self) -> None:
        run = self.run_cycle(recheck_exact=True)
        names = [event["event"] for event in run.events]
        self.assertEqual(run.ensured[0], engine.START_STEPS)
        self.assertLess(names.index("fn.draft.start"), names.index("types.refreshed"))
        self.assertEqual(run.drafts, {"alpha": 2, "beta": 1})  # the edited draft is compared again, not replaced
        self.assertEqual(run.compares, {"alpha": 2, "beta": 2})
        self.assertEqual(run.landed_inside_publish, [True, True])
        self.assertEqual(run.requests, 3)  # cycle start, then once per land
        self.assertEqual(sorted(run.result.data["landed"]), ["alpha", "beta"])
        self.assertEqual(run.result.data["regressed"], [])
        refreshed = next(e for e in run.events if e["event"] == "types.refreshed")
        self.assertEqual(refreshed["steps"], ["types", "headers"])

    def test_a_land_that_no_longer_matches_after_a_refresh_is_a_named_refusal(self) -> None:
        run = self.run_cycle(recheck_exact=False)
        result = run.result
        checks = [event for event in run.events if event["event"] == "fn.recheck"]
        self.assertTrue(checks and not any(event["exact"] for event in checks))
        self.assertEqual(result.key, "cycle.regressed")
        self.assertEqual({row["function"] for row in result.data["regressed"]}, {event["function"] for event in checks})
