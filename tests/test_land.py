"""Land intake, ordering, draining and check orchestration without native work."""

import json
import os
from contextlib import nullcontext
from dataclasses import asdict, replace
from hashlib import sha256
from pathlib import Path
from unittest.mock import Mock

import fixture
import pytest

from unbake import config as configuration
from unbake import land
from unbake.contracts import (
    Config,
    Finding,
    Host,
    LayoutMap,
    Member,
    Placement,
    Project,
    Receipt,
    Refusal,
    Snapshot,
    UnitSpec,
    digest,
)


@pytest.fixture
def lane(tmp_path, monkeypatch):
    # Do not use cfg/project: those fixtures create ROMs and invoke git.
    project = Project(tmp_path, "test", "test", "Test", ("a", "b"), "a", "gcc-test",
                      {}, {}, {}, {}, 200, {}, "project")
    host = Host(2, 2, 1024, 1024, 1024, tmp_path / "tools",
                {}, None, 2, 2, 0.8, 2, ("test", "test@example.invalid"), {}, "host")
    cfg = Config(project, host, "config")
    unit = UnitSpec("src/f.c", "c", "group", ("f",), "gcc-test", {})
    placements = tuple(Placement(v, ".text", 4096, 4104, 0x80000400) for v in ("a", "b"))
    member = Member("f", "function", "asm", "group", placements)
    mapping = LayoutMap(200, {}, {"f": member}, {}, "layout", (), {})
    snapshot = Snapshot(cfg, "a" * 40, mapping, {}, {}, "snapshot")
    capture = Mock(return_value=snapshot)
    logs = Mock()
    stages = []

    def stage(name):
        stages.append(name)
        return nullcontext()

    monkeypatch.setattr(land.layout, "capture", capture)
    monkeypatch.setattr(land.effort, "stage", stage)
    monkeypatch.setattr(land.effort, "invocation", lambda: "test-invocation")
    monkeypatch.setattr(land.store, "log", logs)
    publisher = Mock(side_effect=lambda cfg, entry: Receipt(
        entry.operation, "b" * 40, "plan", entry.proofs, 0, "test-invocation"))
    monkeypatch.setattr(land.publish, "land", publisher)
    return cfg, unit, snapshot, capture, logs, None, publisher, stages


def _request(function=None, note="technique"):
    return {"file": "candidate.c", "function": function,
            "overrides": {"add": [], "omit": []}, "note": note}


def _proofs(unit, exact=True, score=None, symptoms=None):
    return tuple(fixture.proof(unit.path, unit.members[0], v, exact,
                               () if exact else ("bytes differ",), score, symptoms)
                 for v in ("a", "b"))


def _submit(lane, source=b"void f(void) {}\n", **kwargs):
    cfg, unit = lane[:2]
    return land.submit(cfg, _request(), unit, _proofs(unit), source, "submit", **kwargs)


def _paths(cfg, entry, directory=""):
    root = cfg.project.root / ".unbake/inbox" / directory
    return root / f"{entry.id}.json", root / f"{entry.id}.c"


def _order(cfg, entries, times=None):
    for index, entry in enumerate(entries):
        at = times[index] if times else index + 1
        os.utime(_paths(cfg, entry)[0], ns=(at, at))


def test_submit_exact_writes_inbox_pair(lane, monkeypatch):
    cfg, unit, snapshot = lane[:3]
    source = b"void f(void) {}\n"
    replacements = []
    real_replace = os.replace

    def track(before, after):
        replacements.append((before, after))
        real_replace(before, after)

    monkeypatch.setattr(land.store.Path, "replace", lambda self, target: track(self, target))
    entry = _submit(lane, source)
    expected_id = digest(("f", sha256(source).hexdigest(), _request()["overrides"], "publish"))
    assert entry.id == expected_id
    assert entry.member == "f" and entry.function is None
    assert entry.base == snapshot.commit
    assert entry.proofs == _proofs(unit)
    assert entry.origin == "submit" and entry.note == "technique"
    assert entry.invocation == "test-invocation"
    record, copied = _paths(cfg, entry)
    assert copied.read_bytes() == source
    assert json.loads(record.read_text()) == json.loads(json.dumps(asdict(entry)))
    configuration.validate("submission", json.loads(record.read_text()), str(record))
    assert [after for _, after in replacements] == [copied, record]  # each written atomically, by name
    assert not list(record.parent.glob("*.part"))
    assert "land.submit" in lane[-1]


@pytest.mark.parametrize("directory", ["", "done"])
def test_submit_idempotent_same_id(lane, directory):
    cfg = lane[0]
    first = _submit(lane)
    paths = _paths(cfg, first)
    if directory:
        destination = paths[0].parent / directory
        destination.mkdir()
        for path in paths:
            os.replace(path, destination / path.name)
    before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns)
              for p in _paths(cfg, first, directory)}
    second = land.submit(cfg, _request(note="changed"), lane[1], _proofs(lane[1]),
                         b"void f(void) {}\n", "compare")
    assert second == first
    assert isinstance(second.proofs, tuple)
    assert all(isinstance(p.missing, tuple) for p in second.proofs)
    assert before == {p.name: (p.read_bytes(), p.stat().st_mtime_ns)
                      for p in _paths(cfg, first, directory)}
    if directory:
        assert not _paths(cfg, first)[0].exists()


def test_submit_of_a_refused_source_is_queued_again(lane):
    cfg = lane[0]
    first = _submit(lane)
    destination = _paths(cfg, first)[0].parent / "refused"
    destination.mkdir()
    for path in _paths(cfg, first):
        os.replace(path, destination / path.name)
    second = land.submit(cfg, _request(), lane[1], _proofs(lane[1]), b"void f(void) {}\n", "compare")
    assert second.id == first.id
    assert all(path.is_file() for path in _paths(cfg, first))


@pytest.mark.parametrize("kind,same,expected_none", [
    ("c", True, True), ("c", False, False), ("asm", True, False),
])
def test_submit_already_landed_returns_none(lane, kind, same, expected_none):
    cfg, unit, snapshot, capture = lane[:4]
    current = replace(unit, kind=kind)
    capture.return_value = replace(snapshot, layout=replace(snapshot.layout, units={unit.path: current}))
    path = cfg.project.root / unit.path
    path.parent.mkdir()
    path.write_bytes(b"void f(void) {}\n" if same else b"old source")
    result = _submit(lane)
    assert (result is None) == expected_none
    if expected_none:
        assert not (cfg.project.root / ".unbake/inbox").exists()


@pytest.mark.parametrize("score,symptoms,accepted", [
    (0.49, {"bytes_differ": True}, False),
    (0.5, {"bytes_differ": True}, True),
    (0.7, {"compile_failed": False}, False),
    (0.7, {"unresolved": []}, False),
    (0.7, {"no_measurement": False}, False),
])
def test_submit_fuzzy_needs_gain_and_compile(lane, score, symptoms, accepted):
    cfg, unit, snapshot, capture = lane[:4]
    gain = configuration.load_resource("flow.toml")["fuzzy"]["min_gain"]
    fuzzy = {"f": {"path": "src/fuzzy/f.c", "scores": {"a": 0.5 - gain, "b": 0.8}}}
    capture.return_value = replace(snapshot, layout=replace(snapshot.layout, fuzzy=fuzzy))
    proofs = (_proofs(unit, False, 0.9)[0], _proofs(unit, False, score, symptoms)[1])
    result = land.submit(cfg, _request(), unit, proofs, b"candidate", "compare")
    assert (result is not None) == accepted
    if accepted:
        assert result.operation == "fuzzy"
        assert result.id == digest(("f", sha256(b"candidate").hexdigest(),
                                    _request()["overrides"], "fuzzy"))


def test_submit_fuzzy_without_previous_or_proofs(lane):
    cfg, unit = lane[:2]
    assert land.submit(cfg, _request(), unit, (), b"candidate", "compare") is None
    assert land.submit(cfg, _request(), unit, _proofs(unit, False, 0.5),
                       b"candidate", "compare").operation == "fuzzy"


@pytest.mark.parametrize("corrupt", ["{bad json", '{"id": 3}'])
def test_inbox_order_and_corrupt_entry_moved(lane, corrupt):
    cfg = lane[0]
    entries = [_submit(lane, source) for source in (b"one", b"two", b"three")]
    _order(cfg, entries, [20, 10, 10])
    root = _paths(cfg, entries[0])[0].parent
    (root / "broken.json").write_text(corrupt)
    (root / "broken.c").write_bytes(b"broken")
    expected = sorted(entries, key=lambda entry: (_paths(cfg, entry)[0].stat().st_mtime_ns, entry.id))
    assert land.inbox(cfg) == expected
    assert not (root / "broken.json").exists()
    assert (root / "refused/broken.json").read_text() == corrupt
    assert (root / "refused/broken.c").read_bytes() == b"broken"
    body = lane[4].call_args.args[2]
    assert lane[4].call_args.args[1] == "refusal"
    assert body["id"] == "broken"
    assert body["findings"][0]["key"] == "inbox.corrupt"
    assert body["findings"][0]["path"] == ".unbake/inbox/broken.json"


def test_drain_lands_in_order_and_moves_files(lane):
    cfg = lane[0]
    entries = [_submit(lane, source) for source in (b"one", b"two")]
    _order(cfg, entries)
    result = land.drain(cfg)
    assert result == {"running": False, "refused": [], "landed": [
        {"id": e.id, "member": "f", "operation": "publish", "commit": "b" * 40} for e in entries]}
    assert [call.args[1] for call in lane[6].call_args_list] == entries
    for entry in entries:
        for path in _paths(cfg, entry):
            assert not path.exists()
        assert all(path.is_file() for path in _paths(cfg, entry, "done"))
    assert [call.args[1] for call in lane[4].call_args_list] == ["receipt", "receipt"]
    assert lane[4].call_args_list[0].args[2] == {
        "id": entries[0].id, "member": "f",
        **asdict(Receipt("publish", "b" * 40, "plan", entries[0].proofs, 0, "test-invocation"))}
    assert "land.drain" in lane[-1] and lane[-1].count("land.entry") == 2


def test_drain_refusal_recorded_and_continues(lane):
    cfg = lane[0]
    first, second = [_submit(lane, source) for source in (b"one", b"two")]
    _order(cfg, [first, second])
    findings = (Finding("land.not_exact", "mismatch", missing=("bytes",)),
                Finding("land.request", "invalid request"))
    lane[6].side_effect = [Refusal(*findings), Receipt("publish", "b" * 40, "plan", (), 0, "test")]
    result = land.drain(cfg)
    refused = {"id": first.id, "member": "f", "findings": [asdict(f) for f in findings]}
    assert result["refused"] == [refused]
    assert result["landed"][0]["id"] == second.id
    assert not result["running"]
    assert all(p.is_file() for p in _paths(cfg, first, "refused"))
    assert all(p.is_file() for p in _paths(cfg, second, "done"))
    assert lane[4].call_args_list[0].args == (cfg, "refusal", refused)


def test_drain_moves_a_crashed_entry_to_refused_naming_the_exception(lane):
    cfg = lane[0]
    first, second = [_submit(lane, source) for source in (b"one", b"two")]
    _order(cfg, [first, second])
    lane[6].side_effect = [KeyError("boom"), Receipt("publish", "b" * 40, "plan", (), 0, "test")]
    result = land.drain(cfg)
    finding, = result["refused"][0]["findings"]
    assert finding["key"] == "internal.error" and "KeyError" in finding["reason"] and "boom" in finding["reason"]
    assert all(p.is_file() for p in _paths(cfg, first, "refused"))
    assert result["landed"][0]["id"] == second.id and not result["running"]


def test_drain_running_returns_immediately(lane):
    cfg = lane[0]
    entry = _submit(lane)
    # Independent open file descriptions contend under real flock in one process.
    # A child process is forbidden for this module by the phase-2 lane rules.
    with land.store.exclusive(cfg, "land", wait=False) as held:
        assert held
        assert land.drain(cfg) == {"running": True, "landed": [], "refused": []}
    lane[6].assert_not_called()
    assert all(p.exists() for p in _paths(cfg, entry))


def test_drain_picks_up_entry_added_after_listing(lane):
    cfg = lane[0]
    first = _submit(lane, b"first")
    added = []

    def publish(cfg, entry):
        if entry.id == first.id:
            added.append(_submit(lane, b"added"))
        return Receipt("publish", "b" * 40, "plan", (), 0, "test")

    lane[6].side_effect = publish
    result = land.drain(cfg)
    assert [row["id"] for row in result["landed"]] == [first.id, added[0].id]
    assert land.inbox(cfg) == []
    assert all(p.exists() for p in _paths(cfg, added[0], "done"))


def test_drain_rescans_after_lock_release(lane, monkeypatch):
    cfg = lane[0]
    first = _submit(lane, b"first")
    added = []
    real_exclusive = land.store.exclusive
    from contextlib import contextmanager

    @contextmanager
    def exclusive(cfg, name, *, wait):
        with real_exclusive(cfg, name, wait=wait) as held:
            yield held
        if not added:
            added.append(_submit(lane, b"after unlock"))

    monkeypatch.setattr(land.store, "exclusive", exclusive)
    result = land.drain(cfg)
    assert [row["id"] for row in result["landed"]] == [first.id, added[0].id]
    assert lane[6].call_count == 2


def test_drain_measures_again_when_head_moved_under_it(lane):
    cfg = lane[0]
    entry = _submit(lane)
    moved = Refusal(Finding("journal.changed", "HEAD moved"))
    lane[6].side_effect = [moved, Receipt("publish", "b" * 40, "plan", (), 0, "test")]
    result = land.drain(cfg)
    assert [row["id"] for row in result["landed"]] == [entry.id] and result["refused"] == []
    assert lane[6].call_count == 2


def test_drain_refuses_after_the_attempts_when_head_keeps_moving(lane):
    cfg = lane[0]
    _submit(lane)
    lane[6].side_effect = Refusal(Finding("journal.changed", "HEAD moved"))
    assert len(land.drain(cfg)["refused"]) == 1
    assert lane[6].call_count == land._ATTEMPTS


def _flags(**overrides):
    return {"strict": False, **overrides}


@pytest.mark.parametrize("schema", ["result.submit", "result.land", "result.check"])
def test_results_validate(lane, monkeypatch, schema):
    cfg, unit, snapshot = lane[:3]
    if schema == "result.submit":
        file = cfg.project.root / "candidate.c"
        file.write_bytes(b"candidate")
        binder = Mock(return_value=(unit, snapshot))
        measure = Mock(return_value=_proofs(unit))
        gaps = Mock(return_value=())
        monkeypatch.setattr(land.compare, "bind", binder)
        monkeypatch.setattr(land.compare, "measure", measure)
        monkeypatch.setattr(land.compare, "gaps", gaps)
        result = land.submit_command(cfg, {"files": [str(file)], "function": None, "note": None})
        binder.assert_called_once_with(snapshot, file, None)
        measure.assert_called_once_with(snapshot, unit, {"add": [], "omit": []}, None)
        gaps.assert_called_once_with(snapshot, unit, _proofs(unit))
        row = result["submissions"][0]
        assert row["member"] == "f" and row["operation"] == "publish"
        assert row["exact"] and row["score"] == 1.0
        assert result["drain"]["landed"][0]["id"] == row["id"]
    elif schema == "result.land":
        _submit(lane)
        lane[6].side_effect = Refusal(Finding("land.request", "refused"))
        result = land.land_command(cfg, {})
        assert len(result["refused"]) == 1
    else:
        monkeypatch.setattr(land.policy, "census", lambda snap: ())
        result = land.check_command(cfg, _flags())
        assert set(result) == {"commit", "counts"}
    configuration.validate(schema, json.loads(json.dumps(result)), schema)


@pytest.mark.parametrize("files,function", [([], None)])
def test_submit_command_rejects_invalid_request(lane, files, function):
    with pytest.raises(Refusal) as error:
        land.submit_command(lane[0], {"files": files, "function": function, "note": ""})
    assert error.value.findings[0].key == "land.request"
    lane[3].assert_not_called()


def test_check_strict_reports_debt_after_census_written(lane, monkeypatch):
    finding = Finding("check.debt", "debt", blocking=False)
    monkeypatch.setattr(land.policy, "census", lambda snap: (finding,))
    with pytest.raises(Refusal) as error:
        land.check_command(lane[0], _flags(strict=True))
    assert error.value.findings[0].key == "check.debt"
    assert error.value.findings[0].missing == ("check.debt",)
    assert (lane[0].project.root / ".unbake/check.json").is_file()


def test_drain_measures_again_when_another_lane_moved_head(lane):
    cfg = lane[0]
    entry = _submit(lane)
    moved = Refusal(Finding("journal.changed", "HEAD moved"))
    lane[6].side_effect = [moved, moved, Receipt("publish", "b" * 40, "plan", (), 0, "test")]
    result = land.drain(cfg)
    assert [r["id"] for r in result["landed"]] == [entry.id] and result["refused"] == []
    assert lane[6].call_count == land._ATTEMPTS == 3


def test_drain_refuses_after_the_attempts_run_out_and_never_retries_other_refusals(lane):
    cfg = lane[0]
    first, second = [_submit(lane, source) for source in (b"one", b"two")]
    _order(cfg, [first, second])
    moved = Refusal(Finding("journal.changed", "HEAD moved"))
    lane[6].side_effect = [moved] * land._ATTEMPTS + [Refusal(Finding("land.request", "bad"))]
    result = land.drain(cfg)
    assert [r["findings"][0]["key"] for r in result["refused"]] == ["journal.changed", "land.request"]
    assert lane[6].call_count == land._ATTEMPTS + 1


def test_change_set_is_one_entry_with_its_files_and_moves_as_one(lane):
    cfg = lane[0]
    entry = _submit(lane, extras={"include/x.h": b"extern int g;\n", "src/g.c": b"int g;\n"})
    assert sorted(entry.extras) == ["include/x.h", "src/g.c"]
    assert {(cfg.project.root / copy).read_bytes() for copy in entry.extras.values()} == {
        b"extern int g;\n", b"int g;\n"}
    assert _submit(lane).id != entry.id
    assert [r["id"] for r in land.drain(cfg)["landed"]].count(entry.id) == 1
    done = cfg.project.root / ".unbake/inbox/done"
    assert all((cfg.project.root / copy).exists() is False and (done / Path(copy).name).exists()
               for copy in entry.extras.values())
