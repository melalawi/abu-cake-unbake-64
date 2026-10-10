"""store: lock-free cache, append-only streams, exclusive locks and scratch dirs."""

from __future__ import annotations

import multiprocessing
import threading
import time
from pathlib import Path

import pytest

from unbake import effort, pool, store
from unbake.contracts import Config, Refusal


def _hold_child(config: Config, started: Path, release: Path) -> None:
    with store.exclusive(config, "land", wait=False) as held:
        started.write_text(str(held))
        while not release.exists():
            time.sleep(0.02)


def _wait(path: Path) -> None:
    deadline = time.monotonic() + 10
    while not path.exists():
        assert time.monotonic() < deadline, f"{path} never appeared"
        time.sleep(0.02)


def test_cached_pool_cached_holds_no_lock(cfg: Config) -> None:
    """A miss may fan out to the pool whose jobs read and write the same cache: nothing may be held meanwhile."""
    def job(item: int) -> bytes:
        return store.cached(cfg, "inner", str(item), lambda: b"inner")
    def produce() -> bytes:
        return b"+".join(pool.map(cfg, "inner", job, [1]))
    assert store.cached(cfg, "outer", "k", produce) == b"inner"
    assert store.cached(cfg, "outer", "k", lambda: pytest.fail("a second call must hit")) == b"inner"


def test_cached_counts_and_put_overwrites(cfg: Config) -> None:
    before = effort.counters().get("kind", (0, 0))
    assert store.cached(cfg, "kind", "a", lambda: b"one") == b"one"
    assert store.cached(cfg, "kind", "a", lambda: b"two") == b"one"
    after = effort.counters()["kind"]
    assert (after[0] - before[0], after[1] - before[1]) == (1, 1)
    store.put(cfg, "kind", "a", b"three")
    assert store.cached(cfg, "kind", "a", lambda: b"four") == b"three"
    assert effort.counters()["kind"] == (after[0] + 1, after[1])


def test_all_content_entry_points_key_the_installed_code(cfg: Config, monkeypatch) -> None:
    cache = store.content(cfg)
    for code in ("code-a", "code-b"):
        monkeypatch.setattr(store, "CODE", code)
        assert cache.get("externs", "same") is None
        assert ("externs", "same") not in cache
        assert cache.cached("externs", "same", lambda value=code: value.encode()) == code.encode()
        assert ("externs", "same") in cache
        assert cache.get("externs", "same") == code.encode()
        cache.put("single", "same", code.encode())
        cache.put_many([("batch", "same", code.encode())])
    monkeypatch.setattr(store, "CODE", "code-a")
    assert cache.cached("externs", "same", lambda: pytest.fail("code-a must hit")) == b"code-a"
    assert store.get(cfg, "single", "same") == b"code-a"
    assert store.get(cfg, "batch", "same") == b"code-a"
    assert set(cache._cache) == {f"{kind}:{code}:same" for kind in ("externs", "single", "batch")
                               for code in ("code-a", "code-b")}


def test_append_rows_roundtrip(cfg: Config) -> None:
    store.append(cfg, "attempts/f", {"b": 1, "a": "é"})
    store.append(cfg, "attempts/f", {"n": [1, 2]})
    path = cfg.project.root / ".unbake" / "attempts" / "f.jsonl"
    assert path.read_text().splitlines()[0] == '{"a":"\\u00e9","b":1}'
    assert store.rows(cfg, "attempts/f") == [{"a": "é", "b": 1}, {"n": [1, 2]}]


def test_rows_missing_is_empty(cfg: Config) -> None:
    assert store.rows(cfg, "nothing") == []


def test_rows_bad_line_refuses_with_line(cfg: Config) -> None:
    store.append(cfg, "s", {"ok": 1})
    path = cfg.project.root / ".unbake" / "s.jsonl"
    with path.open("a") as handle:
        handle.write("{broken\n")
    with pytest.raises(Refusal) as caught:
        store.rows(cfg, "s")
    finding = caught.value.findings[0]
    assert finding.key == "store.corrupt"
    assert finding.line == 2 and finding.path == str(path) and f"{path}:2:" in finding.reason


@pytest.mark.parametrize(
    ("stream", "ok"),
    [("attempts/f", True), ("ledger", True), ("a.b-c_d/e.f", True), ("../x", False), ("a/b/c", False),
     ("", False), ("a b", False), ("/abs", False), ("a/", False), ("a/..", False)],
)
def test_stream_name_rules(cfg: Config, stream: str, ok: bool) -> None:
    if ok:
        store.append(cfg, stream, {"v": 1})
        assert store.rows(cfg, stream) == [{"v": 1}]
    else:
        with pytest.raises(Refusal) as caught:
            store.append(cfg, stream, {"v": 1})
        assert caught.value.findings[0].key == "store.corrupt"


def test_log_kinds(cfg: Config) -> None:
    store.log(cfg, "receipt", {"x": 1})
    (row,) = store.rows(cfg, "ledger")
    assert row["schema"] == 2 and row["kind"] == "receipt" and row["body"] == {"x": 1}
    assert row["invocation"] == effort.invocation() and row["time"]
    with pytest.raises(Refusal) as caught:
        store.log(cfg, "bogus", {})
    assert caught.value.findings[0].key == "store.corrupt"
    assert len(store.rows(cfg, "ledger")) == 1


def test_exclusive_second_holder_gets_false(cfg: Config, tmp_path: Path) -> None:
    started, release = tmp_path / "started", tmp_path / "release"
    proc = multiprocessing.get_context("fork").Process(target=_hold_child, args=(cfg, started, release))
    proc.start()
    try:
        _wait(started)
        assert started.read_text() == "True"
        with store.exclusive(cfg, "land", wait=False) as held:
            assert held is False
    finally:
        release.write_text("go")
        proc.join(10)
    assert proc.exitcode == 0
    with store.exclusive(cfg, "land", wait=False) as held:
        assert held is True
        text = (cfg.project.root / ".unbake" / "land.lock").read_text()
        assert text.strip().split()[0].isdigit() and text.endswith("\n")


def test_exclusive_waits_for_a_held_lock_and_records_the_wait(cfg: Config, tmp_path: Path, monkeypatch) -> None:
    started, release = tmp_path / "started", tmp_path / "release"
    proc = multiprocessing.get_context("fork").Process(target=_hold_child, args=(cfg, started, release))
    proc.start()
    waits: list[float] = []
    monkeypatch.setattr(effort, "waited", waits.append)
    try:
        _wait(started)
        threading.Timer(0.3, release.write_text, ["go"]).start()
        with store.exclusive(cfg, "land", wait=True) as held:
            assert held is True
    finally:
        proc.join(10)
    assert len(waits) == 1


def test_work_dir_removed(cfg: Config) -> None:
    with store.work(cfg) as path:
        assert path.is_dir() and path.parent == cfg.project.root / ".unbake" / "tmp"
        (path / "f").write_text("x")
        kept = path
    assert not kept.exists()
    with pytest.raises(RuntimeError), store.work(cfg) as path:
        failed = path
        raise RuntimeError
    assert not failed.exists()


def test_flock_charges_only_blocked_time(tmp_path, monkeypatch):
    calls = []
    def flock(fd, flags):
        calls.append(flags)
        if len(calls) == 1 and flags & store.fcntl.LOCK_NB:
            raise BlockingIOError
    monkeypatch.setattr(store.fcntl, "flock", flock)
    with effort.command("check", []):
        with effort.stage("blocked"), store._flock(tmp_path / "a.lock", store.fcntl.LOCK_EX):
            pass
        with effort.stage("free"), store._flock(tmp_path / "b.lock", store.fcntl.LOCK_EX):
            pass
    waits = {r.path[-1]: r.wait_seconds for r in effort.closed() if r.kind == "stage"}
    assert waits["blocked"] > 0 and waits["free"] == 0
    assert calls[1] == store.fcntl.LOCK_EX


def test_stem_flattens_slashes_and_digests_long_names():
    assert store.stem("src/a.c") == "src.a.c"
    long = "x/" + "y" * 200
    assert len(store.stem(long)) <= 100 and store.stem(long) != store.stem(long + "z")


def test_write_keeps_a_name_near_the_filesystem_limit(tmp_path: Path) -> None:
    path = tmp_path / ("x" * 240 + ".s")  # the temporary file must not lengthen the name past 255 bytes
    assert store.write(path, b"a") and path.read_bytes() == b"a" and [p.name for p in tmp_path.iterdir()] == [path.name]


def _budget_child(config, pipe, release):
    with store.slots(config, 10) as width:
        pipe.send(width)
        release.wait(20)


def test_two_processes_share_ten_worker_slots(cfg):
    from dataclasses import replace
    config = replace(cfg, host=replace(cfg.host, cores=10, workers=10))
    context = multiprocessing.get_context("spawn")
    receive, send = context.Pipe(False)
    release = context.Event()
    with store.slots(config, 4) as own:
        children = [context.Process(target=_budget_child, args=(config, send, release)) for _ in range(2)]
        for child in children:
            child.start()
        try:
            other = receive.recv()
            assert own + other == 10
            with pytest.raises(Refusal, match=r"resources\.workers"):
                changed = replace(config, host=replace(config.host, workers=11))
                # Another process/thread must inspect the directory rather than reuse our lease.
                token = store._held.set(None)
                try:
                    with store.slots(changed, 1):
                        pass
                finally:
                    store._held.reset(token)
        finally:
            release.set()
            for child in children:
                child.join(20)
        assert receive.recv() == 6
        assert all(child.exitcode == 0 for child in children)
    with store.slots(config, 10) as width:
        assert width == 10


def test_nested_budget_uses_existing_slot_and_failure_releases(cfg):
    with pytest.raises(ValueError), store.slots(cfg, 1) as width:
        with store.slots(cfg, cfg.host.workers) as nested:
            assert width == nested == 1
        raise ValueError("failed")
    with store.slots(cfg, cfg.host.workers) as width:
        assert width == cfg.host.workers


def test_budget_capacity_is_checked_once_per_process_without_serializing_jobs(cfg, monkeypatch):
    from dataclasses import replace

    locks = []
    actual = store._flock
    def flock(path, *args, **kwargs):
        locks.append(path.name)
        return actual(path, *args, **kwargs)
    monkeypatch.setattr(store, "_flock", flock)
    for _ in range(20):
        with store.slots(cfg, 1) as width:
            assert width == 1
    assert locks.count("allocate.lock") == 1 and locks.count("0") == 20
    changed = replace(cfg, host=replace(cfg.host, workers=cfg.host.workers + 1))
    with pytest.raises(Refusal, match=r"resources\.workers"), store.slots(changed, 1):
        pass
    assert locks.count("allocate.lock") == 2
    # A fork inherits the memo but must validate independently, before leasing.
    monkeypatch.setattr(store.os, "getpid", lambda: -1)
    with store.slots(cfg, 1):
        pass
    assert locks.count("allocate.lock") == 3
