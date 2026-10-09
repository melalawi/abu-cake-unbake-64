"""The shared performance mechanisms count work: warm items never dispatch, readers run once, the watchdog speaks."""
from dataclasses import replace
from pathlib import Path

import pytest
from test_effort import _config

from unbake import effort, pool, process
from unbake.contracts import Origin, Refusal


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    config = _config(tmp_path)
    origins = {**config.host.origins, **{f"budgets.{k}": Origin(k, "/host.toml", "0" * 64) for k in (
        "serial_seconds", "serial_cores", "pool_fill", "pool_fanout")}}
    monkeypatch.setattr(pool, "_in_worker", True)  # jobs run in this process: the count is what is under test
    monkeypatch.setattr(effort, "_listeners", [])
    return replace(config, host=replace(config.host, origins=origins))


def test_a_keyed_map_dispatches_only_its_misses(cfg):
    ran = []
    def job(item):
        ran.append(item)
        return item * 2
    with effort.command("check", []):
        assert pool.map(cfg, "doubles", job, [1, 2, 3], str) == [2, 4, 6]
        assert pool.map(cfg, "doubles", job, [1, 2, 3, 4], str) == [2, 4, 6, 8]
        assert pool.map(cfg, "doubles", job, [1, 2, 3, 4], str) == [2, 4, 6, 8]
    assert ran == [1, 2, 3, 4]  # the warm map dispatched 0 items
    assert [r.jobs for r in effort.closed() if r.kind == "pool"] == [3, 1, 0]


def test_a_none_key_always_dispatches(cfg):
    ran = []
    with effort.command("check", []):
        for _ in range(2):
            pool.map(cfg, "open", ran.append, [1], lambda item: None)
    assert ran == [1, 1]


def test_gather_runs_independent_groups_in_one_dispatch_loop(cfg):
    with effort.command("check", []):
        found = pool.gather(cfg, [("squares", lambda v: v * v, [1, 2, 3]), ("names", str, ["a"], str)])
    assert found == [[1, 4, 9], ["a"]]
    assert len([r for r in effort.closed() if r.path[-1] == "pool.map"]) == 1


def test_memo_computes_once_per_command_and_resets(cfg):
    calls = []
    def produce():
        calls.append(1)
        return len(calls)
    with effort.command("check", []):
        assert [effort.memo("k", produce), effort.memo("k", produce)] == [1, 1]
    with effort.command("check", []):
        assert effort.memo("k", produce) == 2


def _budgeted(cfg, *changes):
    """Each change is applied to one finished stage of a bound command and run through the watchdog in turn."""
    with effort.command("check", []):
        effort.bind(cfg)
        with effort.stage("work"):
            pass
        base = next(r for r in effort.closed() if r.path[-1] == "work")
        return [effort._budget(replace(base, **change), True) for change in changes]


def test_budget_flags_a_single_core_stage(cfg):
    (record,) = _budgeted(cfg, {"wall_seconds": 5.0, "cores": 0.5})
    assert [f.key for f in record.findings] == ["budget.single_core"]
    assert not record.findings[0].blocking


def test_budget_flags_an_underused_pool_and_a_warm_dispatch(cfg):
    (record,) = _budgeted(cfg, {"kind": "pool", "jobs": 40, "workers_admitted": 2, "cores": 0.5,
                                "cache": {"x": (3, 0)}})
    assert {f.key for f in record.findings} == {"budget.pool_underused", "budget.warm_dispatch"}


def test_budget_flags_a_stage_that_closes_twice(cfg):
    first, second = _budgeted(cfg, {"wall_seconds": 5.0, "cores": 3.0}, {"wall_seconds": 5.0, "cores": 3.0})
    assert not first.findings and [f.key for f in second.findings] == ["budget.repeated"]


@pytest.mark.parametrize("tool", ["make", "permuter"])
def test_process_refuses_a_parallel_tool_without_jobs(tmp_path, tool):
    executable = tmp_path / tool
    executable.write_text("#!/bin/sh\n")
    executable.chmod(0o755)
    with pytest.raises(Refusal) as refused:
        process.run("tool", [str(executable), "arg"], tmp_path, tmp=tmp_path / "tmp")
    assert tool in refused.value.findings[0].reason and Path(executable).is_file()


def test_peek_reads_a_file_or_says_it_is_absent(tmp_path):
    from unbake.contracts import Snapshot
    config = _config(tmp_path)
    (config.project.root / "here.txt").write_bytes(b"x")
    snapshot = Snapshot(config, "head", None, {}, {"over.txt": b"y", "gone.txt": None}, "digest")
    assert [snapshot.peek(p) for p in ("here.txt", "over.txt", "gone.txt", "absent.txt")] == [b"x", b"y", None, None]
