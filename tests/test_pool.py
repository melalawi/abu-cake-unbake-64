"""Bounded ordered maps, metrics, and progress using futures and forkserver."""
import io
import os
import pickle
import sys
import threading
import time
from concurrent.futures import Future
from concurrent.futures.process import BrokenProcessPool
from dataclasses import replace
from pathlib import Path

import pytest
from test_effort import _config

from unbake import effort, pool, view
from unbake.contracts import Finding, Refusal

_RAISED = ValueError("unset")


def _fail(value):  # module level: items and functions travel to workers through a pickle
    raise _RAISED


_RAN: list[int] = []


def _record(value):
    _RAN.append(value)  # inline in the parent here: the pool runs a group of two in this process or a worker
    return value


def _name(value):
    return str(value)


def _square(value):
    return value * value


@pytest.fixture(autouse=True)
def _state(monkeypatch):
    monkeypatch.setattr(effort, "_listeners", [])
    monkeypatch.setattr(pool, "_in_worker", False)
    yield
    pool._drop_executor()


@pytest.fixture
def mocked_parallel(monkeypatch):
    active = set()
    widths = []
    class Executor:
        def submit(self, function, *args):
            future = Future()
            future.set_result(function(*args))
            active.add(future)
            widths.append(len(active))
            return future
    def wait(pending, return_when):
        selected = next(reversed(pending))
        active.discard(selected)
        return {selected}, set(pending) - {selected}
    monkeypatch.setattr(pool, "_get_executor", lambda config: Executor())
    monkeypatch.setattr(pool, "wait", wait)
    return widths


@pytest.mark.parametrize("serial", [True, False])
def test_progress_counts(tmp_path, monkeypatch, mocked_parallel, serial):
    monkeypatch.setattr(pool, "_in_worker", serial)
    seen = []
    effort.listen(lambda event, body: seen.append(body) if event == "progress" else None)
    with effort.command("check", []):
        assert pool.map(_config(tmp_path), "units", _square, [1, 2, 3, 4]) == [1, 4, 9, 16]
    assert seen == [{"name": "units", "done": n, "total": 4} for n in range(1, 5)]
    if not serial:
        assert max(mocked_parallel) == min(4, 2 * pool._AHEAD)  # queued ahead of the workers, never more


@pytest.mark.parametrize("items", [[], [3]])
def test_inline_no_executor(tmp_path, monkeypatch, items):
    def forbidden(config):
        pytest.fail("inline map started an executor")
    monkeypatch.setattr(pool, "_get_executor", forbidden)
    with effort.command("check", []):
        assert pool.map(_config(tmp_path), "units", _square, items) == [v * v for v in items]
    record = next(r for r in effort.closed() if r.kind == "pool")
    assert record.items == record.jobs == len(items)
    assert record.workers_admitted == 0


@pytest.mark.parametrize("exc,key", [
    (Refusal(Finding("effort.log", "refused")), "effort.log"),
    (MemoryError("limit"), "worker.memory"), (ValueError("crash"), "worker.crash"),
])
def test_worker_failure_mapping(tmp_path, mocked_parallel, monkeypatch, exc, key):
    monkeypatch.setattr(sys.modules[__name__], "_RAISED", exc)
    with effort.command("check", []), pytest.raises(Refusal) as caught:
        pool.map(_config(tmp_path), "units", _fail, [1, 2])
    assert caught.value.findings[0].key == key
    if key == "effort.log":
        assert caught.value is exc
    if key == "worker.memory":
        assert caught.value.findings[0].origin == _config(tmp_path).host.origins["resources.memory_worker_bytes"]
    assert next(r for r in effort.closed() if r.kind == "pool").jobs == 2


def test_job_cache_and_metrics(tmp_path):
    def work(value):
        effort.count("object", True)
        effort.count("view", False)
        return value
    ok, value, envelope = pool._job(work, 7, 2.0)
    assert (ok, value) == (True, 7)
    assert envelope["cache"] == {"object": (1, 0), "view": (0, 1)}
    assert envelope["worker_cpu"] >= 0
    assert envelope["rss"] > 0
    assert envelope["records"] == []  # quick, uneventful jobs ship no stage records


def test_broken_pool_dropped(tmp_path, monkeypatch, mocked_parallel):
    dropped = []
    def broken(pending, return_when):
        raise BrokenProcessPool("worker gone")
    monkeypatch.setattr(pool, "wait", broken)
    monkeypatch.setattr(pool, "_drop_executor", lambda: dropped.append(True))
    with effort.command("check", []), pytest.raises(Refusal) as caught:
        pool.map(_config(tmp_path), "units", _square, [1, 2])
    assert caught.value.findings[0].key == "worker.crash"
    assert dropped == [True]


def test_executor_reuse_and_host_change(tmp_path, monkeypatch):
    made = []
    class Executor:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.shutdowns = []
            made.append(self)
        def shutdown(self, **kwargs):
            self.shutdowns.append(kwargs)
    monkeypatch.setattr(pool, "ProcessPoolExecutor", Executor)
    config = _config(tmp_path)
    first = pool._get_executor(config)
    assert pool._get_executor(config) is first
    second = pool._get_executor(replace(config, host=replace(config.host, digest="other")))
    assert second is not first
    assert first.shutdowns == [{"wait": True, "cancel_futures": True}]
    assert second.kwargs["max_workers"] == config.host.workers
    assert second.kwargs["mp_context"].get_start_method() == "forkserver"
    assert second.kwargs["initargs"] == (config.host.memory_worker_bytes,)


def test_threads_asking_at_once_create_one_executor(tmp_path, monkeypatch):
    made = []
    class Executor:
        def __init__(self, **kwargs):
            time.sleep(0.05)  # long enough for every other thread to arrive while this one is creating
            made.append(self)
        def shutdown(self, **kwargs):
            pass
    monkeypatch.setattr(pool, "ProcessPoolExecutor", Executor)
    config = _config(tmp_path)
    got = []
    threads = [threading.Thread(target=lambda: got.append(pool._get_executor(config))) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(made) == 1 and got == made * 8


def test_real_forkserver_map(tmp_path, monkeypatch):
    seen = []
    effort.listen(lambda event, body: seen.append(body["done"]) if event == "progress" else None)
    with effort.command("check", []):
        assert pool.map(_config(tmp_path), "units", _square, [2, 3, 4]) == [4, 9, 16]
    assert seen == [1, 2, 3]
    record = next(r for r in effort.closed() if r.kind == "pool")
    assert record.jobs == 3
    assert 1 <= record.workers_used <= record.workers_admitted == 2

def test_large_map_batches_transport_and_preserves_every_metric(tmp_path, mocked_parallel):
    items = list(range(256))
    with effort.command("check", []):
        values = pool.map(_config(tmp_path), "units", _square, items)
    assert values == [v * v for v in items]
    record = next(r for r in effort.closed() if r.kind == "pool")
    assert record.items == record.jobs == len(items)
    assert record.workers_admitted == 2


def _identify(value):
    return value, os.getpid()


def test_real_batched_work_count_order_and_worker_pids(tmp_path, monkeypatch):
    items = list(range(256))
    with effort.command("check", []):
        values = pool.map(_config(tmp_path), "units", _identify, items)
    assert [value for value, pid in values] == items
    assert all(pid != os.getpid() for value, pid in values)
    metrics = next(r for r in effort.closed() if r.kind == "pool")
    assert metrics.jobs == metrics.items == len(items)
    assert metrics.workers_used == len({pid for value, pid in values})
    assert metrics.workers_admitted == 2
    assert all(r.wait_seconds for r in effort.closed() if r.kind == "job")


def test_job_ships_only_notable_records(tmp_path):
    def work(value):
        with effort.stage("quick"):
            pass
        with effort.stage("waited"):
            effort.waited(0.5)
        return value
    _, _, kept = pool._job(work, 1, 1000.0)
    assert [r["path"][-1] for r in kept["records"]] == ["waited"]
    _, _, everything = pool._job(work, 1, 0.0)
    assert {r["path"][-1] for r in everything["records"]} == {"quick", "waited", "job", "(own)"}


def test_items_travel_without_the_snapshot_they_run_against(tmp_path, monkeypatch):
    """A snapshot is megabytes: it is stored once in the cache by digest and every worker loads it once."""
    import hashlib
    import pickle

    from unbake.contracts import LayoutMap, Member, Placement, Snapshot
    members = {f"m{i}": Member(f"m{i}", "function", "asm", "g", (Placement("a", ".text", i, i + 4, i, 0),))
               for i in range(2000)}
    snapshot = Snapshot(_config(tmp_path), "head", LayoutMap(32, {}, members, {}, "layout", (), {}), {}, {}, "digest-1")
    stream = io.BytesIO()
    pool._Pickler(stream, protocol=5).dump((_square, [(snapshot, 1), (snapshot, 2)]))
    key = hashlib.sha256((str(tmp_path) + "digest-1").encode()).hexdigest()  # copies of one project stay apart
    assert len(stream.getvalue()) < 5000 and pool.store.get(snapshot.config, "snapshot", key) is not None
    loads = []
    stored = pickle.dumps(snapshot)
    monkeypatch.setattr(pool.store, "get", lambda config, kind, key: loads.append((kind, key)) or stored)
    monkeypatch.setattr(pool, "_SNAPSHOTS", {})
    function, items = pool._Unpickler(io.BytesIO(stream.getvalue())).load()
    assert function is _square and items[0][0] == snapshot and items[0][0] is items[1][0]
    pool._Unpickler(io.BytesIO(stream.getvalue())).load()
    assert loads == [("snapshot", key)]  # the second load is the worker's memo


def test_pool_record_measures_the_parent_and_the_workers(tmp_path, mocked_parallel):
    with effort.command("check", []):
        pool.map(_config(tmp_path), "units", _square, list(range(80)))
    record = next(r for r in effort.closed() if r.kind == "pool")
    assert record.dispatch_seconds >= 0 and record.busy_seconds >= 0 and record.jobs == 80


def test_worker_dies_with_its_parent(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(pool.resource, "setrlimit", lambda *a: None)
    monkeypatch.setattr(pool.ctypes, "CDLL", lambda name: type("L", (), {"prctl": lambda self, *a: calls.append(a)})())
    monkeypatch.setattr(pool, "_in_worker", False)
    pool._init(1 << 30)
    assert calls == [(1, pool.signal.SIGKILL)]


def test_forkserver_socket_directory_is_short_and_private(tmp_path, monkeypatch):
    runtime = tmp_path / "run"
    runtime.mkdir()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    monkeypatch.setattr(pool.mp_process.current_process(), "_config", {})
    pool._short_socket_dir()
    chosen = Path(pool.mp_process.current_process()._config["tempdir"])
    assert chosen.parent == runtime and chosen.is_dir()
    pool._short_socket_dir()  # chosen once
    assert Path(pool.mp_process.current_process()._config["tempdir"]) == chosen


def test_forkserver_socket_directory_refuses_without_runtime_dir(tmp_path, monkeypatch):
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.setattr(pool.mp_process.current_process(), "_config", {})
    with pytest.raises(Refusal) as refusal:
        pool._short_socket_dir()
    assert "XDG_RUNTIME_DIR" in refusal.value.findings[0].reason


def test_a_warm_keyed_group_costs_one_store_read(tmp_path, monkeypatch):
    config, reads = _config(tmp_path), []
    real = pool.store.get
    monkeypatch.setattr(pool.store, "get", lambda cfg, kind, key: reads.append(kind) or real(cfg, kind, key))
    items = [1, 2, 3, 4]
    with effort.command("check", []):
        assert pool.map(config, "units", _square, items, key=str) == [1, 4, 9, 16]
    assert reads.count("units") == 4  # the cold run asks for every item
    reads.clear()
    with effort.command("check", []):
        assert pool.map(config, "units", _square, items, key=str) == [1, 4, 9, 16]
        assert effort.counters()["units"] == (4, 0)  # all four still count as hits
    assert reads == ["units.group"]
    reads.clear()
    assert pool.map(config, "units", _square, [1, 2, 3, 5], key=str) == [1, 4, 9, 25]  # one new item: per-item reads
    assert reads.count("units") == 4


def test_whoever_ran_a_keyed_chunk_stores_its_results(tmp_path):
    config = _config(tmp_path)
    outcomes = pool._chunk(_square, [2, 3, 4], 0.0, (config, "units", ["a", None, "c"]))
    assert [o[1] for o in outcomes] == [4, 9, 16]
    assert pickle.loads(pool.store.get(config, "units", "a")) == 4 and pool.store.get(config, "units", "c") is not None
    assert pool.store.get(config, "units", "b") is None  # an unkeyed item is not stored
    failed = pool._chunk(_fail, [1], 0.0, (config, "units", ["f"]))
    assert failed[0][0] is False and pool.store.get(config, "units", "f") is None
    assert [o[1] for o in pool._chunk(_square, [5], 0.0, None)] == [25]


def test_a_worker_crash_names_the_type_and_the_innermost_unbake_frame(tmp_path):
    ok, error, _ = pool._job(view._sha, "not bytes", 0.0)  # hashlib refuses inside unbake.view._sha
    assert not ok and error.__notes__[0].startswith("at view:_sha:")
    with pytest.raises(Refusal) as raised:
        pool._raise_failure(_config(tmp_path), "stage", 3, error)
    assert "TypeError" in raised.value.findings[0].reason and "at view:_sha:" in raised.value.findings[0].reason


def test_an_equal_key_from_a_changed_module_is_cold(tmp_path, monkeypatch, mocked_parallel):
    config, ran = _config(tmp_path), _RAN
    ran.clear()
    monkeypatch.setattr(pool, "_IDENTITIES", {__name__: "one"})
    pool.map(config, "units", _record, [1, 2], key=_name)
    pool.map(config, "units", _record, [1, 2], key=_name)
    assert ran == [1, 2]  # the second run is warm
    monkeypatch.setattr(pool, "_IDENTITIES", {__name__: "two"})
    pool.map(config, "units", _record, [1, 2], key=_name)
    assert ran == [1, 2, 1, 2]


def test_the_key_pass_dispatches_chunks_and_the_parent_computes_no_key(tmp_path, monkeypatch, mocked_parallel):
    config, chunks, parent = _config(tmp_path), [], []
    monkeypatch.setattr(sys.modules[__name__], "_KEY_CHUNKS", chunks)
    monkeypatch.setattr(pool, "_keys_job", _record_key_chunk)
    monkeypatch.setattr(pool, "_keys", lambda key, items, identity: parent.append(len(items)))
    items = list(range(640))
    pool.map(config, "units", _square, items, key=_name)
    assert sum(chunks) == 640 and 1 < len(chunks) < 640 and parent == []


_KEY_CHUNKS = []

def _record_key_chunk(item):
    items = item[1]
    _KEY_CHUNKS.append(len(items))
    return [str(value) for value in items]


def test_the_parent_asks_the_store_for_a_snapshot_once_however_many_chunks_travel(tmp_path, monkeypatch):
    config = _config(tmp_path)
    from unbake.contracts import LayoutMap, Snapshot
    snapshot = Snapshot(config, "commit", LayoutMap(1, {}, {}, {}, "layout", (), {}), {}, {}, "once-snapshot")
    asked, writes = [], []
    monkeypatch.setattr(pool, "_STORED", set())
    monkeypatch.setattr(pool.store, "content", lambda cfg: _Counting(asked))
    monkeypatch.setattr(pool.store, "get", lambda *args: pytest.fail("presence must not read snapshot bytes"))
    monkeypatch.setattr(pool.store, "put", lambda cfg, kind, key, value: writes.append(key))
    ids = [pool._Pickler(io.BytesIO(), protocol=5).persistent_id(snapshot) for _ in range(50)]
    assert len({i[1] for i in ids}) == 1 and len(asked) == 1 and writes == [ids[0][1]]


class _Counting:
    """A content cache stand-in that counts how often the parent asks whether a key is held."""
    def __init__(self, asked):
        self._cache = self
        self.asked = asked
    def __contains__(self, key):
        self.asked.append(key)
        return False
