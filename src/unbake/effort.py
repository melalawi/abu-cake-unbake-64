"""Invocation spans, exclusive parent work, and worker/native accounting."""
from __future__ import annotations

import json
import os
import resource
import time
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager as ContextManager
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, replace
from pathlib import Path
from uuid import uuid4

import psutil

from unbake.contracts import Config, Finding, Json, NativeResult, Origin, Refusal, Span, StageRecord

_stack: ContextVar[tuple[_Open, ...]] = ContextVar("effort_stack", default=())
_closed: list[StageRecord] = []
_counters: dict[str, list[int]] = {}
_config: Config | None = None
_sink: Path | None = None
_buffer: list[StageRecord] = []
_invocation = ""
_memo: dict = {}  # pure readers' values for this command, by input digest
_seen: set[tuple[str, ...]] = set()  # slow stage paths already closed in this command
_listeners: list[Callable[[str, Json], None]] = []
def memo(key: object, produce: Callable[[], object]) -> object:
    """A pure reader's value for the running command: computed once per key, dropped when the command ends."""
    if key not in _memo:
        _memo[key] = produce()
    return _memo[key]
def listen(callback: Callable[[str, Json], None]) -> None:
    _listeners.append(callback)
def _notify(event: str, body: Json) -> None:
    for callback in tuple(_listeners):
        try:
            callback(event, body)
        except Exception as exc:
            if _stack.get():
                _stack.get()[-1].findings.append(Finding("effort.log", reason=f"Listener: {exc}", blocking=False))
def progress(name: str, done: int, total: int) -> None:
    _notify("progress", {"name": name, "done": done, "total": total})
def closed() -> tuple[StageRecord, ...]:
    return tuple(_closed)
def _cpu() -> float:
    usage = resource.getrusage(resource.RUSAGE_SELF)
    return usage.ru_utime + usage.ru_stime
class _Open:
    def __init__(self, name: str, kind: str, parent: _Open | None = None):
        self.span, self.parent = uuid4().hex, parent.span if parent else None
        self.path, self.kind = (parent.path if parent else ()) + (name,), kind
        self.start, self.cpu = time.perf_counter_ns(), _cpu()
        self.rss = psutil.Process(os.getpid()).memory_info().rss if kind in ("command", "pool") else 0
        self.cache, self.items, self.findings, self.children, self.wait = counters(), 0, [], [], 0.0
        self.remote_cache: dict[str, tuple[int, int]] = {}
    def add(self, *, items: int = 0, findings: Sequence[Finding] = ()) -> None:
        self.items += items
        self.findings.extend(findings)
def waited(seconds: float) -> None:
    _stack.get()[-1:] and setattr(_stack.get()[-1], "wait", _stack.get()[-1].wait + seconds)
def _limits() -> dict:
    keys = ("resources.memory_parent_bytes", "resources.memory_worker_bytes")
    return {"memory_limits": {k: _config.host.origins[k] for k in keys},
            "memory_limit_values": dict(zip(keys, (_config.host.memory_parent_bytes,
                                                    _config.host.memory_worker_bytes), strict=True))} if _config else {}
def _record(opened: _Open, wall: float, parent: float = 0, worker: float = 0,
            native: float = 0, **values) -> StageRecord:
    cores = (parent + worker + native) / wall if wall else 0
    execution = "parallel" if _config and cores >= _config.host.serial_cores else "serial"
    values.setdefault("wait_seconds", opened.wait)
    return StageRecord(_invocation, opened.span, opened.parent, opened.path, opened.kind,
                       "ok", execution, opened.start, wall, parent, worker, native, cores,
                       values.pop("items", opened.items), values.pop("jobs", 0),
                       values.pop("workers_used", 0), values.pop("workers_admitted", 0),
                       values.pop("parent_rss_peak_bytes", 0), values.pop("worker_rss_peak_bytes", 0),
                       **(_limits() if opened.parent is None else {}), **values)
def _budget(record: StageRecord, leaf: bool) -> StageRecord:
    """The watchdog: a stage that breaks a [budgets] rule carries a non-blocking budget.* finding naming it."""
    if not _config or record.kind not in ("stage", "own", "pool"):
        return record
    host, found, cores, pool = _config.host, [], f"{record.cores:.2f} cores", record.kind == "pool"
    slow = record.wall_seconds >= host.serial_seconds
    hits, misses = (sum(v[i] for v in record.cache.values()) for i in (0, 1))
    if leaf and slow and record.cores < host.serial_cores:
        found.append(("single_core", "serial_seconds", f"ran {record.wall_seconds:.1f}s at {cores}"))
    if pool and record.jobs >= host.pool_fanout * record.workers_admitted and (
            record.cores < host.pool_fill * record.workers_admitted):
        found.append(("pool_underused", "pool_fill", f"kept {cores} of {record.workers_admitted} workers busy"))
    if pool and record.jobs and hits and not misses:
        found.append(("warm_dispatch", "pool_fill", f"dispatched {record.jobs} jobs with every cache lookup warm"))
    if slow and record.kind != "own":
        if record.path in _seen:
            found.append(("repeated", "serial_seconds", "closed twice in one command"))
        _seen.add(record.path)
    return replace(record, findings=(*record.findings, *(
        Finding(f"budget.{key}", reason=f"{' > '.join(record.path)} {why}", blocking=False,
                origin=host.origins[f"budgets.{name}"]) for key, name, why in found)))
def _log_failure() -> Finding:
    finding = Finding("effort.log", reason="Stage log could not be written.", blocking=False)
    if _stack.get():
        _stack.get()[0].findings.append(finding)
    return finding
def _emit(record: StageRecord, leaf: bool = False) -> StageRecord:
    record = _budget(record, leaf)
    if _sink and (record.parent is None or record.kind == "pool" or record.findings
                  or record.wall_seconds >= _config.host.serial_seconds / 4):
        try:
            with _sink.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(asdict(record)) + "\n")
        except OSError:
            finding = _log_failure()
            if record.parent is None:
                record = replace(record, findings=(*record.findings, finding))
            _buffer.append(record)
    elif not _sink:
        _buffer.append(record)
    _closed.append(record)
    return record
def _close(opened: _Open, status: str) -> StageRecord:
    end = time.perf_counter_ns()
    children = opened.children
    wall = max(0, end - opened.start) / 1e9
    parent = max(0, _cpu() - opened.cpu)
    own_cpu = max(0, parent - sum(c.parent_cpu_seconds for c in children))
    cache = {k: (v[0] - opened.cache.get(k, (0, 0))[0], v[1] - opened.cache.get(k, (0, 0))[1])
             for k, v in counters().items()}
    _merge_cache(cache, opened.remote_cache)
    record = _record(opened, wall, parent, sum(c.worker_cpu_seconds for c in children),
                     sum(c.native_cpu_seconds for c in children), cache=cache,
                     items=opened.items + sum(c.items for c in children if c.kind == "pool"),
                     jobs=sum(c.jobs for c in children),
                     workers_used=max((c.workers_used for c in children), default=0),
                     workers_admitted=max((c.workers_admitted for c in children), default=0),
                     parent_rss_peak_bytes=max(opened.rss, psutil.Process(os.getpid()).memory_info().rss,
                                               resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024),
                     worker_rss_peak_bytes=max((c.worker_rss_peak_bytes for c in children), default=0))
    if children:
        covered, cursor = 0, opened.start
        for start, stop in sorted((max(opened.start, c.start_ns), min(end, c.start_ns + int(c.wall_seconds * 1e9)))
                                  for c in children):
            covered += max(0, stop - max(cursor, start))
            cursor = max(cursor, stop)
        own = _Open.__new__(_Open)
        own.span, own.parent, own.path = uuid4().hex, opened.span, (*opened.path, "(own)")
        own.kind, own.start, own.items, own.wait = "own", opened.start, 0, 0.0
        _emit(replace(_record(own, max(0, wall - covered / 1e9), own_cpu), status=status), True)
    return replace(record, status=status)
@contextmanager
def _scope(opened: _Open):
    token = _stack.set((*_stack.get(), opened))
    status = "ok"
    try:
        if opened.kind in ("command", "stage", "pool"):
            _notify("open", {"path": list(opened.path)})
        yield opened
    except BaseException as exc:
        status = ("refused" if isinstance(exc, Refusal) else
                  "interrupted" if isinstance(exc, KeyboardInterrupt) else "failed")
        if isinstance(exc, Refusal):
            opened.findings.extend(exc.findings)
        raise
    finally:
        try:
            record = _close(opened, status)
            if opened.kind in ("command", "stage", "pool"):
                _notify("close", {"path": list(opened.path), "wall": record.wall_seconds, "status": record.status})
            record = _emit(replace(record, findings=tuple(opened.findings)), not opened.children)
            stack = _stack.get()
            if len(stack) > 1:
                stack[-2].children.append(record)
                _merge_cache(stack[-2].remote_cache, opened.remote_cache)
        finally:
            _stack.reset(token)
@contextmanager
def command(name: str, argv: Sequence[str]) -> ContextManager[None]:
    global _closed, _counters, _config, _sink, _buffer, _invocation, _memo, _seen
    _closed, _counters, _config, _sink, _buffer, _invocation = [], {}, None, None, [], uuid4().hex
    _memo, _seen = {}, set()
    token = _stack.set(())
    try:
        with _scope(_Open(name, "command")):
            yield None
    finally:
        _stack.reset(token)
def bind(config: Config) -> None:
    global _config, _sink, _buffer
    with stage("effort.bind"):
        _config, _sink = config, config.project.root / ".unbake" / "stages.jsonl"
        try:
            _sink.parent.mkdir(parents=True, exist_ok=True)
            with _sink.open("a", encoding="utf-8") as stream:
                while _buffer:
                    stream.write(json.dumps(asdict(_buffer[0])) + "\n")
                    stream.flush()
                    _buffer.pop(0)
        except OSError:
            _log_failure()
@contextmanager
def stage(name: str) -> ContextManager[Span]:
    if not _stack.get():
        yield _Null()
    else:
        with _scope(_Open(name, "stage", _stack.get()[-1])) as span:
            yield span
def record_native(name: str, result: NativeResult, start_ns: int) -> None:
    if _stack.get():
        opened = _Open(name, "native", _stack.get()[-1])
        opened.start = start_ns
        record = _emit(_record(opened, result.wall_seconds, native=result.cpu_seconds,
                               worker_rss_peak_bytes=result.max_rss_bytes))
        _stack.get()[-1].children.append(record)
def record_pool(name: str, items: int, jobs: int, admitted: int, start_ns: int, envelopes: Sequence[Json],
                dispatch_seconds: float = 0.0) -> None:
    if not _stack.get():
        return
    opened = _Open(name, "pool", _stack.get()[-1])
    opened.start = start_ns
    cache: dict[str, tuple[int, int]] = {}
    for envelope in envelopes:
        _merge_cache(cache, envelope.get("cache", {}))
        records = envelope.get("records", [])
        ids = {r["span"]: uuid4().hex for r in records}
        parents = {r["parent"] for r in records}  # a shipped stage nothing else hangs under is a worker's leaf
        for raw in records:
            values = dict(raw)
            values.update(invocation=_invocation, span=ids[raw["span"]], parent=ids.get(raw["parent"], opened.span),
                          path=opened.path + tuple(raw["path"]), parent_cpu_seconds=0,
                          worker_cpu_seconds=raw["parent_cpu_seconds"] + raw["worker_cpu_seconds"],
                          parent_rss_peak_bytes=0,
                          worker_rss_peak_bytes=max(raw["parent_rss_peak_bytes"], raw["worker_rss_peak_bytes"]))
            values["memory_limits"] = {k: Origin(**v) for k, v in raw.get("memory_limits", {}).items()}
            values.update(_limits())
            values["cache"] = {k: tuple(v) for k, v in raw.get("cache", {}).items()}
            values["findings"] = tuple(Finding(**{**f, "versions": tuple(f.get("versions", ())),
                                       "missing": tuple(f.get("missing", ())),
                                       "origin": Origin(**f["origin"]) if f.get("origin") else None})
                                       for f in raw.get("findings", []))
            _emit(StageRecord(**values), raw["span"] not in parents)
    record = _emit(_record(opened, max(0, time.perf_counter_ns() - start_ns) / 1e9,
                           worker=sum(e["worker_cpu"] for e in envelopes),
                           native=sum(e["native_cpu"] for e in envelopes),
                           items=items, jobs=jobs, workers_admitted=admitted, cache=cache,
                           workers_used=len({e["pid"] for e in envelopes}), dispatch_seconds=dispatch_seconds,
                           busy_seconds=sum((e["end_ns"] - e["start_ns"]) / 1e9 for e in envelopes),
                           worker_rss_peak_bytes=max((e["rss"] for e in envelopes), default=0)), True)
    _stack.get()[-1].children.append(record)
    _merge_cache(_stack.get()[-1].remote_cache, cache)
class _Null:
    def add(self, *, items: int = 0, findings: Sequence[Finding] = ()) -> None:
        pass
def _merge_cache(target: dict, source: dict) -> None:
    for key, pair in source.items():
        previous = target.get(key, (0, 0))
        target[key] = (previous[0] + pair[0], previous[1] + pair[1])
def count(kind: str, hit: bool) -> None:
    _counters.setdefault(kind, [0, 0])[0 if hit else 1] += 1
def counters() -> dict[str, tuple[int, int]]:
    return {k: (v[0], v[1]) for k, v in _counters.items()}
@contextmanager
def capture() -> ContextManager[list[StageRecord]]:
    global _closed, _counters, _sink, _buffer, _invocation, _seen
    saved = _closed, _counters, _sink, _buffer, _invocation, _seen
    _closed, _counters, _sink, _buffer, _invocation, _seen = [], {}, None, [], uuid4().hex, set()
    token = _stack.set(())
    try:
        with _scope(_Open("job", "job")):
            yield _closed
    finally:
        _stack.reset(token)
        _closed, _counters, _sink, _buffer, _invocation, _seen = saved
def tree() -> Json:
    nodes = {r.span: {**asdict(r), "children": []} for r in _closed}
    roots = []
    for record in _closed:
        (nodes[record.parent]["children"] if record.parent in nodes else roots).append(nodes[record.span])
    return roots[-1] if roots else {}
def invocation() -> str:
    return _invocation
