"""Bounded worker execution with ordered results and retained job metrics."""
from __future__ import annotations

import ctypes
import hashlib
import io
import os
import pickle
import resource
import shutil
import signal
import tempfile
import time
from collections.abc import Callable, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ProcessPoolExecutor, wait
from concurrent.futures.process import BrokenProcessPool
from dataclasses import asdict
from multiprocessing import get_context, util
from multiprocessing import process as mp_process
from pathlib import Path
from typing import Any

from unbake import effort, store
from unbake.contracts import Config, Finding, Json, Refusal, Snapshot, digest

_AHEAD = 3  # chunks queued per worker, so a worker that finishes one starts the next without waiting for the parent
_executor: ProcessPoolExecutor | None = None
_host_digest: str | None = None
_in_worker = False
def _init(limit: int) -> None:
    global _in_worker
    resource.setrlimit(resource.RLIMIT_DATA, (limit, limit))
    ctypes.CDLL(None).prctl(1, signal.SIGKILL)  # PR_SET_PDEATHSIG: a worker never outlives the process that forked it
    _in_worker = True
def _short_socket_dir() -> None:
    """The fork server's socket must fit an AF_UNIX path (about 100 bytes), so its directory is a short private one
    under the user's runtime directory, never under the project path or the shared temp directory."""
    config = mp_process.current_process()._config
    if config.get("tempdir"):
        return
    base = os.environ.get("XDG_RUNTIME_DIR") or ""
    if not base or not Path(base).is_dir():
        raise Refusal(Finding("worker.crash", "XDG_RUNTIME_DIR is not a directory: the worker pool needs a short "
                              "private directory for its socket", path=base))
    directory = tempfile.mkdtemp(prefix="ub-", dir=base)
    config["tempdir"] = directory
    util.Finalize(None, shutil.rmtree, args=(directory, True), exitpriority=-100)
def _drop_executor() -> None:
    global _executor, _host_digest
    executor, _executor = _executor, None
    _host_digest = None
    if executor is not None:
        executor.shutdown(wait=True, cancel_futures=True)
def _get_executor(config: Config) -> ProcessPoolExecutor:
    global _executor, _host_digest
    if _executor is not None and _host_digest != config.host.digest:
        _drop_executor()
    if _executor is None:
        _short_socket_dir()
        context = get_context("forkserver")
        context.set_forkserver_preload(["unbake.deathwatch"])  # the server dies with this process, the workers with it
        _executor = ProcessPoolExecutor(
            max_workers=config.host.workers,
            mp_context=context,
            initializer=_init,
            initargs=(config.host.memory_worker_bytes,),
        )
        _host_digest = config.host.digest
    return _executor
def _job(function: Callable[[Any], Any], item: Any, floor: float) -> tuple[bool, Any, Json]:
    start_ns = time.monotonic_ns()
    before = resource.getrusage(resource.RUSAGE_SELF)
    with effort.capture() as records:
        try:
            value, ok = function(item), True
        except Exception as exc:
            value, ok = exc, False
    after = resource.getrusage(resource.RUSAGE_SELF)
    return ok, value, {
        "pid": os.getpid(),
        "worker_cpu": after.ru_utime + after.ru_stime - before.ru_utime - before.ru_stime,
        "native_cpu": sum(r.native_cpu_seconds for r in records if r.kind == "native"),
        "rss": after.ru_maxrss * 1024,
        "cache": dict(records[-1].cache),
        "records": [asdict(r) for r in records if r.wall_seconds >= floor or r.findings or r.wait_seconds],
        "start_ns": start_ns,
        "end_ns": time.monotonic_ns(),
    }
_SNAPSHOTS: dict[str, Snapshot] = {}  # per worker: the snapshots it has loaded, by digest
_STORED: set[str] = set()  # in the parent: the snapshots already in the content cache
class _Pickler(pickle.Pickler):
    """Items travel without the snapshot they run against: it is stored once in the content cache by digest."""
    def persistent_id(self, obj: Any) -> tuple | None:
        if isinstance(obj, Snapshot):
            key = hashlib.sha256((str(obj.config.project.root) + obj.digest).encode()).hexdigest()
            if key not in _STORED and store.get(obj.config, "snapshot", key) is None:
                store.put(obj.config, "snapshot", key, pickle.dumps(obj, protocol=5))
            _STORED.add(key)
            return ("snapshot", key, obj.config)
        return None
class _Unpickler(pickle.Unpickler):
    def persistent_load(self, pid: tuple) -> Snapshot:
        _, digest, config = pid
        if digest not in _SNAPSHOTS:
            if len(_SNAPSHOTS) >= 4:
                del _SNAPSHOTS[next(iter(_SNAPSHOTS))]
            value = store.get(config, "snapshot", digest)
            if value is None:
                raise Refusal(Finding("worker.crash", f"snapshot {digest[:12]} is missing from the cache"))
            _SNAPSHOTS[digest] = pickle.loads(value)
        return _SNAPSHOTS[digest]
def _chunk(function: Callable, items: Sequence, floor: float, sink: tuple | None) -> list[tuple[bool, Any, Json]]:
    outcomes = [_job(function, item, floor) for item in items]
    if sink:
        store.put_many(sink[0], sink[1], [(key, pickle.dumps(value, protocol=5)) for (ok, value, _), key in
                                          zip(outcomes, sink[2], strict=True) if ok and key is not None])
    return outcomes
def _dispatch(blob: bytes, floor: float) -> list[tuple[bool, Any, Json]]:
    """One chunk travels as its own pickle, so a worker only unpacks the items it runs and stores their results."""
    function, items, sink = _Unpickler(io.BytesIO(blob)).load()
    return _chunk(function, items, floor, sink)
def _raise_failure(config: Config, name: str, index: int, exc: Any) -> None:
    if isinstance(exc, Refusal):
        raise exc
    memory = isinstance(exc, MemoryError)
    raise Refusal(Finding(
        "worker.memory" if memory else "worker.crash",
        reason=f"Stage {name} item {index} " + (f"exceeded worker memory: {exc!r}" if memory else f"failed: {exc!r}"),
        origin=config.host.origins["resources.memory_worker_bytes"] if memory else None,
    )) from exc
class _Run:
    """One group of a gather: its items, the cache keys of those, and the indexes the workers must still compute."""
    def __init__(self, config: Config, name: str, function: Callable, items: Sequence,
                 key: Callable[[Any], str | None] | None = None):
        self.name, self.function, self.items, self.key = name, function, items, key
        self.outcomes: list[tuple[bool, Any, Json] | None] = [None] * len(items)
        self.keys = [key(item) for item in items] if key else []
        self.todo = list(range(len(items)))
        self.whole = digest(self.keys) if key and None not in self.keys else None
        if key:  # the parent resolves hits itself: a warm item never travels to a worker
            blob = self.whole and store.get(config, name + ".group", self.whole)  # a fully warm group is one entry
            warm = pickle.loads(blob) if blob else [k and store.get(config, name, k) for k in self.keys]
            for index, found in enumerate(warm):
                effort.count(name, found is not None)
                if found is not None:
                    self.outcomes[index] = (True, pickle.loads(found), {})
            self.todo = [i for i, outcome in enumerate(self.outcomes) if outcome is None]
        self.submitted, self.done = 0, len(items) - len(self.todo)
    def seal(self, config: Config) -> None:
        if self.whole and self.todo and all(o and o[0] for o in self.outcomes):
            blobs = [pickle.dumps(o[1], protocol=5) for o in self.outcomes]
            store.put(config, self.name + ".group", self.whole, pickle.dumps(blobs))
    def sink(self, config: Config, picked: Sequence[int]) -> tuple | None:
        return (config, self.name, [self.keys[i] for i in picked]) if self.key else None
    def settle(self, picked: Sequence[int], outcomes: Sequence[tuple[bool, Any, Json]]) -> None:
        for index, outcome in zip(picked, outcomes, strict=True):
            self.outcomes[index] = outcome
        self.done += len(picked)
        effort.progress(self.name, self.done, len(self.items))
def map(config: Config, name: str, function: Callable[[Any], Any], items: Sequence[Any],
        key: Callable[[Any], str | None] | None = None) -> list[Any]:
    return gather(config, [(name, function, items, key)])[0]
def gather(config: Config, groups: Sequence[tuple]) -> list[list[Any]]:
    """Runs independent (name, function, items[, key]) groups in one dispatch loop, so a small group never idles the
    workers. A keyed group resolves its cache hits in the parent and dispatches only its misses."""
    with effort.stage("pool.map"):
        start_ns, waited, current = time.monotonic_ns(), 0.0, (0, 0)
        runs = [_Run(config, *group) for group in groups]
        total, admitted = sum(len(r.todo) for r in runs), 0
        try:
            inline = total <= 1 or _in_worker  # nothing to fan out: the jobs run here, one at a time
            width, floor, pending = 1 if inline else config.host.workers, config.host.serial_seconds, {}
            admitted = 0 if inline else width
            executor = None if inline else _get_executor(config)
            size = {r: 1 if inline else max(1, len(r.todo) // (width * 32)) for r in runs}
            tasks = ((r, lo) for r in runs for lo in range(0, len(r.todo), size[r]))
            while True:
                while len(pending) < width * _AHEAD and (task := next(tasks, None)):
                    run, lo = task
                    picked = run.todo[lo:lo + size[run]]
                    if inline:
                        pending[future := Future()] = (run, picked)
                        future.set_result(_chunk(run.function, [run.items[i] for i in picked], floor,
                                                 run.sink(config, picked)))
                    else:
                        stream = io.BytesIO()
                        _Pickler(stream, protocol=5).dump(
                            (run.function, [run.items[i] for i in picked], run.sink(config, picked)))
                        pending[executor.submit(_dispatch, stream.getvalue(), floor)] = (run, picked)
                    run.submitted += len(picked)
                if not pending:
                    break
                blocked = time.perf_counter()
                completed, _ = wait(pending, return_when=FIRST_COMPLETED)
                waited += time.perf_counter() - blocked
                for future in completed:
                    run, picked = pending.pop(future)
                    current = (runs.index(run), picked[0])
                    try:
                        outcomes = future.result()
                    except BrokenProcessPool:
                        raise
                    except Exception as exc:
                        outcomes = [(False, exc, {})] * len(picked)
                    run.settle(picked, outcomes)
        except BrokenProcessPool as exc:
            _drop_executor()
            _raise_failure(config, runs[current[0]].name, current[1], exc)
        finally:
            for run in runs:
                effort.record_pool(
                    run.name, len(run.items), jobs=run.submitted, admitted=admitted, start_ns=start_ns,
                    envelopes=[o[2] for o in run.outcomes if o and o[2]],
                    dispatch_seconds=(time.monotonic_ns() - start_ns) / 1e9 - waited,
                )
        for run in runs:
            run.seal(config)
            for index, outcome in enumerate(run.outcomes):
                if outcome is not None and not outcome[0]:
                    _raise_failure(config, run.name, index, outcome[1])
        return [[outcome[1] for outcome in run.outcomes] for run in runs]
