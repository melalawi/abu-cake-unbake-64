"""Running TaskIdentity, median work deadlines, and cleanup through the real process pool."""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import multiprocessing
import pickle
import threading
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tests.kit import TempCase
from tests.ledger_fixture import fault_evidence
from unbake import effort, pool
from unbake.typemap import declarations, facts

FIXTURE = Path(__file__).parent / "typemap/fixtures/ragewars_worker_unit.c"
KEYS = (
    "806f5db67d220631bab65e2e5603557f584a28f09297dc36d3cd56f2e09f8223",
    "e423aa14d10e4b4061e86fcb8fbd0dd92cc38547e6e847566ea0ce68eef9363f",
    "db0d1f9bbdc045e42cd17d73f3b51fc4a25c462dd85cf25292a0680519b67f3b",
    "529029ef9da2f8e492e1ff6a850d0aa56efa9c4b2d5041c36cc910069dff4114",
    "3dc62885347e4544894229b790d7584016207e2e21ced171428bb41bebecf72e",
)


def identity(source: str = "src/func_8028F544_de.c") -> pool.TaskIdentity:
    data = FIXTURE.read_bytes()
    return pool.TaskIdentity(
        "types",
        source,
        ("func_8028F544_de",),
        ("de", "eu", "eu-x", "us", "us-rev1"),
        len(data),
        hashlib.sha256(data).hexdigest(),
        KEYS,
    )


class Clock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value

    def advance(self, work: float) -> None:
        self.value += work


def task_identity(shared, item):
    return identity("src/" + item["source"].name)


def task(item):
    """One counted file scan, or a deliberately non-terminating real facts._unit_job body."""
    if item["kind"] == "normal":
        data = item["source"].read_bytes()
        if hasattr(pool, "progress"):
            pool.progress(step="sample")
        return {"scans": 1, "bytes": len(data)}
    item["gate"].recv()

    def never_finishes(shared, versions):
        marker = shared[0].root / "blocked-starts"
        with marker.open("a") as output:
            output.write("one unit body\n")
        if hasattr(pool, "progress"):
            pool.progress(step="blocked")
        item["ready"].send("unit body started")
        threading.Event().wait()
        raise AssertionError("a blocked task cannot finish")

    facts._unit_work = never_finishes
    return facts._unit_job(item["shared"], item["versions"])


task._pool_identity = task_identity


class MedianTests(TempCase):
    def test_queued_work_is_not_running_and_real_work_resets_the_deadline(self):
        clock = Clock()
        dog = pool.Watchdog(clock=clock, multiplier=4, minimum=1, bootstrap=20)
        dog.queued("finished", identity("first.c"), "reading-c")
        dog.queued("queued", identity("queued.c"), "reading-c")
        clock.advance(100)
        self.assertEqual(dog.expired(), [])
        dog.update("finished", "start", 100, None, "item")
        clock.advance(2)
        dog.update("finished", "item-done", 100, None, "sendback")
        dog.finished("finished")
        dog.queued("running", identity(), "reading-c")
        dog.update("running", "start", 101, None, "item")
        clock.advance(7)
        self.assertEqual(dog.expired(), [])
        dog.update("running", "progress", 101, None, "version:us")
        clock.advance(8)
        [(token, failure)] = dog.expired()
        self.assertEqual(token, "running")
        self.assertEqual(failure.failure, "worker.stuck")
        self.assertEqual(fault_evidence(failure.fault)["identity"], pool.asdict(identity()))
        self.assertEqual(fault_evidence(failure.fault)["phase_median_seconds"], 2)
        self.assertEqual(fault_evidence(failure.fault)["threshold_seconds"], 8)
        self.assertEqual(fault_evidence(failure.fault)["stage"], "version:us")
        self.assertEqual(len(dog.samples["reading-c"]), 1)
        self.assertIsNone(dog.current["queued"].pid)

    def test_the_active_unit_inside_a_batch_replaces_the_first_identity(self):
        clock = Clock()
        dog = pool.Watchdog(clock=clock, bootstrap=10, multiplier=4, minimum=1)
        dog.queued("batch", identity("first.c"), "reading-c")
        dog.update("batch", "start", 123, None, "item")
        clock.advance(2)
        dog.update("batch", "item-done", 123, None, "sendback")
        dog.update("batch", "start", 123, identity("second.c"), "item")
        clock.advance(8)
        [(_, failure)] = dog.expired()
        self.assertEqual(fault_evidence(failure.fault)["identity"]["source"], "second.c")
        self.assertNotIn("first.c", failure.reason)
        self.assertNotIn("retry failed", failure.reason)
        # Sendback is still running until the actual future completes.
        dog.update("batch", "item-done", 123, None, "sendback")
        self.assertIsNotNone(dog.current["batch"].pid)
        dog.finished("batch")
        clock.advance(1000)
        self.assertEqual(dog.expired(), [])

    def test_startup_is_bounded_and_samples_are_bounded(self):
        clock = Clock()
        dog = pool.Watchdog(clock=clock, bootstrap=3)
        dog.queued("one", identity(), "phase")
        dog.update("one", "start", 100, None, "item")
        clock.advance(3)
        self.assertEqual(len(dog.expired()), 1)
        for _ in range(100):
            dog.update("one", "start", 100, None, "item")
            clock.advance(1)
            dog.update("one", "item-done", 100, None, "sendback")
        self.assertEqual(len(dog.samples["phase"]), 64)

    def test_large_identity_transport_is_one_atomic_small_message_and_one_file(self):
        events = []
        large = pool.TaskIdentity(
            "types", "real.c", tuple("function_" + str(i) for i in range(500)), (), 12, "a" * 64, ()
        )
        with (
            patch.object(pool, "_token", "submission"),
            patch.object(pool, "_events", SimpleNamespace(put=events.append)),
            patch.object(pool, "_progress_root", str(self.root)),
        ):
            pool._notify("start", "item", large)
        self.assertEqual(len(events), 1)
        self.assertLess(len(events[0]), 3000)
        kind, path = pickle.loads(events[0])
        self.assertEqual(kind, "file")
        self.assertEqual(len(list(self.root.iterdir())), 1)
        token, state, _, actual, stage = pickle.loads(Path(path).read_bytes())
        self.assertEqual((token, state, actual, stage), ("submission", "start", large, "item"))

    def test_synthetic_declared_unit_keeps_provenance_hash_version_and_input_key(self):
        # The real route6 evidence identity, reduced C input; no claim that this slice reproduces its OOM.
        project = SimpleNamespace(root=self.root)
        provenance = {
            "kind": "declared",
            "version": "us",
            "source": "declaration_evidence",
            "sha256": "15634c5f0d54764e20fb0b725569de50e67a9b2e44be3ebe7867ea8bdee74e8a",
        }
        text = "typedef int s32;\nextern s32 retained_word;\n"
        found = declarations._declared_identity(None, (project, None, text, provenance, set()))
        self.assertEqual(found.source, "declaration_evidence")
        self.assertEqual(found.source_sha256, provenance["sha256"])
        self.assertEqual(found.versions, ("us",))
        self.assertEqual(found.source_bytes, len(text.encode()))
        self.assertEqual(found.input_keys, (facts.text_key(text, provenance, []),))


class NativeTests(TempCase):
    def source(self, name):
        source = self.root / "src" / name
        source.parent.mkdir(exist_ok=True)
        source.write_bytes(FIXTURE.read_bytes())
        return source

    def test_nonterminating_real_unit_fails_at_four_median_work_steps_and_keeps_completed_work(self):
        normal = self.source("normal.c")
        source = self.source("func_8028F544_de.c")
        gate, waiter = multiprocessing.Pipe()
        received, sender = multiprocessing.Pipe()
        shared = (SimpleNamespace(root=self.root), None, {}, frozenset())
        versions = [
            [(i, key, ("func_8028F544_de", source, version))]
            for i, (key, version) in enumerate(zip(KEYS, identity().versions, strict=True))
        ]
        clock = Clock()
        dog = pool.Watchdog(clock=clock, multiplier=4, minimum=1, bootstrap=10) if hasattr(pool, "Watchdog") else None
        workers = pool.Pool(2, 3_000_000_000, 500_000_000, 512_000_000, self.root / "transport")
        if dog is not None:
            workers.watchdog = dog
        records = []
        stderr = io.StringIO()
        before = effort.counted().get("worker.stuck", (0, 0))
        try:
            with contextlib.redirect_stderr(stderr), workers:
                if dog is not None:
                    report = workers._report

                    def observed(token, state, current):
                        records.append((state, current.identity.source, current.stage))
                        report(token, state, current)
                        if state == "progress" and current.stage == "sample":
                            clock.advance(1)
                        if state == "progress" and current.stage == "blocked":
                            clock.advance(4)

                    workers._report = observed
                results = workers.map(
                    task,
                    [
                        {"kind": "normal", "source": normal},
                        {
                            "kind": "blocked",
                            "source": source,
                            "gate": waiter,
                            "ready": sender,
                            "shared": shared,
                            "versions": versions,
                        },
                    ],
                )
                self.assertEqual(next(results), {"scans": 1, "bytes": source.stat().st_size})
                if dog is not None:
                    with workers._lock:
                        dog.samples[workers._phase] = deque([1.0], maxlen=64)
                gate.send("one completed scan")
                self.assertEqual(received.recv(), "unit body started")
                if dog is None:
                    # Historical main really entered the non-terminating native task;
                    # fail on the missing bounded behavior, then context cleanup kills it.
                    clock.advance(4)
                    self.fail("native unit still running after four median work steps; no TaskIdentity watchdog")
                with self.assertRaises(pool.TaskFailed) as raised:
                    next(results)
                failure = raised.exception
                self.assertEqual(failure.failure, "worker.stuck")
                self.assertEqual(fault_evidence(failure.fault)["identity"], pool.asdict(identity()))
                self.assertEqual(fault_evidence(failure.fault)["threshold_seconds"], 4)
                self.assertEqual(fault_evidence(failure.fault)["phase_median_seconds"], 1)
                self.assertEqual(fault_evidence(failure.fault)["stage"], "blocked")
                self.assertEqual((self.root / "blocked-starts").read_text(), "one unit body\n")
                self.assertEqual(sum(state == "stuck" for state, _, _ in records), 1)
        finally:
            for connection in (gate, waiter, received, sender):
                connection.close()
        after = effort.counted()["worker.stuck"]
        self.assertEqual(tuple(a - b for a, b in zip(after, before, strict=True)), (1, 1))
        self.assertIsNone(workers._monitor)
        self.assertEqual(list((self.root / "transport").glob("progress-*")), [])
        shown = [json.loads(line) for line in stderr.getvalue().splitlines() if line.startswith("{")]
        self.assertTrue(any(row["state"] == "queued" and row["pid"] is None for row in shown))
        self.assertTrue(
            any(row["state"] == "running" and row["identity"]["source"] == identity().source for row in shown)
        )

    def test_native_batches_report_each_inner_identity_without_extra_workers_or_scans(self):
        sources = [self.source(name) for name in ("alpha.c", "beta.c", "gamma.c")]
        workers = pool.Pool(2, 3_000_000_000, 500_000_000, 512_000_000, self.root / "transport")
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), patch.object(pool, "width", return_value=2), workers:
            result = workers.run(task, [{"kind": "normal", "source": source} for source in sources])
        self.assertEqual(result, [{"scans": 1, "bytes": source.stat().st_size} for source in sources])
        shown = [json.loads(line) for line in stderr.getvalue().splitlines() if line.startswith("{")]
        running = [row for row in shown if row["state"] == "running" and row["stage"] == "item"]
        self.assertEqual(
            [row["identity"]["source"] for row in running if row["identity"]["source"] == "src/beta.c"], ["src/beta.c"]
        )
        self.assertEqual({row["identity"]["source"] for row in running}, {"src/" + source.name for source in sources})
        self.assertLessEqual(len({row["pid"] for row in running}), 2)
        self.assertEqual(list((self.root / "transport").glob("progress-*")), [])


class MoreNativeTests(TempCase):
    source = NativeTests.source

    def test_stuck_second_item_in_one_native_batch_names_second_identity_once(self):
        normal = self.source("first.c")
        source = self.source("func_8028F544_de.c")
        gate, waiter = multiprocessing.Pipe()
        received, sender = multiprocessing.Pipe()
        shared = (SimpleNamespace(root=self.root), None, {}, frozenset())
        versions = [
            [(i, key, ("func_8028F544_de", source, version))]
            for i, (key, version) in enumerate(zip(KEYS, identity().versions, strict=True))
        ]
        clock = Clock()
        dog = pool.Watchdog(clock=clock, multiplier=4, minimum=1, bootstrap=10)
        workers = pool.Pool(2, 3_000_000_000, 500_000_000, 512_000_000, self.root / "transport", watchdog=dog)
        done = []
        try:
            with contextlib.redirect_stderr(io.StringIO()), patch.object(pool, "width", return_value=2), workers:
                original = workers._report

                def observed(token, state, current):
                    original(token, state, current)
                    if state == "progress" and current.stage == "sample":
                        clock.advance(1)
                    if state == "item-done" and current.identity.source == "src/first.c":
                        done.append(current.identity)
                        gate.send("one counted completion")
                    if state == "progress" and current.stage == "blocked":
                        clock.advance(4)

                workers._report = observed
                with self.assertRaises(pool.TaskFailed) as raised:
                    workers.run(
                        task,
                        [
                            {"kind": "normal", "source": normal},
                            {
                                "kind": "blocked",
                                "source": source,
                                "gate": waiter,
                                "ready": sender,
                                "shared": shared,
                                "versions": versions,
                            },
                        ],
                    )
                self.assertEqual(received.recv(), "unit body started")
                self.assertEqual(fault_evidence(raised.exception.fault)["identity"], pool.asdict(identity()))
                self.assertNotIn("first.c", raised.exception.reason)
                self.assertEqual(len(done), 1)
                self.assertEqual((self.root / "blocked-starts").read_text(), "one unit body\n")
        finally:
            for connection in (gate, waiter, received, sender):
                connection.close()

    def test_standalone_future_is_watched_without_a_map_consumer(self):
        normal = self.source("normal.c")
        source = self.source("func_8028F544_de.c")
        gate, waiter = multiprocessing.Pipe()
        received, sender = multiprocessing.Pipe()
        shared = (SimpleNamespace(root=self.root), None, {}, frozenset())
        versions = [
            [(i, key, ("func_8028F544_de", source, version))]
            for i, (key, version) in enumerate(zip(KEYS, identity().versions, strict=True))
        ]
        clock = Clock()
        dog = pool.Watchdog(clock=clock, multiplier=4, minimum=1, bootstrap=10)
        workers = pool.Pool(2, 3_000_000_000, 500_000_000, 512_000_000, self.root / "transport", watchdog=dog)
        try:
            with contextlib.redirect_stderr(io.StringIO()), workers:
                original = workers._report

                def observed(token, state, current):
                    original(token, state, current)
                    if state == "progress" and current.stage == "sample":
                        clock.advance(1)
                    if state == "progress" and current.stage == "blocked":
                        clock.advance(4)

                workers._report = observed
                good = workers.submit(task, {"kind": "normal", "source": normal})
                bad = workers.submit(
                    task,
                    {
                        "kind": "blocked",
                        "source": source,
                        "gate": waiter,
                        "ready": sender,
                        "shared": shared,
                        "versions": versions,
                    },
                )
                self.assertEqual(good.result(), {"scans": 1, "bytes": source.stat().st_size})
                with workers._lock:
                    dog.samples[workers._phase] = deque([1.0], maxlen=64)
                gate.send("one completed scan")
                self.assertEqual(received.recv(), "unit body started")
                with self.assertRaises(pool.TaskFailed) as raised:
                    bad.result()
                self.assertEqual(fault_evidence(raised.exception.fault)["identity"], pool.asdict(identity()))
                self.assertEqual((self.root / "blocked-starts").read_text(), "one unit body\n")
        finally:
            for connection in (gate, waiter, received, sender):
                connection.close()


class TerminalOrderTests(TempCase):
    def test_stuck_later_task_refuses_without_waiting_for_an_earlier_live_future(self):
        from concurrent.futures import Future
        from concurrent.futures.process import BrokenProcessPool

        live, failed = Future(), Future()
        failed.set_exception(BrokenProcessPool())
        fault = pool.TaskFailed(
            "worker.stuck",
            {
                "action": "types",
                "identity": pool.asdict(identity()),
                "cause": "no task progress",
                "retry_exhausted": False,
            },
        )
        workers = pool.Pool(2, 3_000_000_000, 500_000_000, 512_000_000)
        with (
            patch.object(workers, "_submit", side_effect=[live, failed]) as submitted,
            patch.object(workers, "_failure", side_effect=lambda future, *args: fault if future is failed else None),
            patch.object(workers, "_fresh") as fresh,
            self.assertRaises(pool.TaskFailed) as raised,
        ):
            list(workers.map(int, [0, 1]))
        self.assertIs(raised.exception, fault)
        self.assertFalse(live.done())
        self.assertEqual(submitted.call_count, 2)
        fresh.assert_not_called()

    def test_a_completed_later_result_is_kept_and_charged_once_on_immediate_refusal(self):
        from concurrent.futures import Future
        from concurrent.futures.process import BrokenProcessPool

        failed, completed = Future(), Future()
        failed.set_exception(BrokenProcessPool())
        completed.set_result((7, 1.0, 0, {"fixture.scans": (1, 1)}, None))
        fault = pool.TaskFailed(
            "worker.stuck",
            {
                "action": "types",
                "identity": pool.asdict(identity()),
                "cause": "no task progress",
                "retry_exhausted": False,
            },
        )
        workers = pool.Pool(2, 3_000_000_000, 500_000_000, 512_000_000)
        before = effort.counted().get("fixture.scans", (0, 0))
        with (
            patch.object(workers, "_submit", side_effect=[failed, completed]),
            patch.object(workers, "_failure", side_effect=lambda future, *args: fault if future is failed else None),
            self.assertRaises(pool.TaskFailed) as raised,
        ):
            list(workers.map(int, [0, 1]))
        self.assertEqual([value for _, value in raised.exception.completed], [7])
        self.assertTrue(all(isinstance(named, pool.TaskIdentity) for named, _ in raised.exception.completed))
        after = effort.counted()["fixture.scans"]
        self.assertEqual(tuple(a - b for a, b in zip(after, before, strict=True)), (1, 1))
