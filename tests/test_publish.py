
"""Publish lane: real contract records, mocked native/pool/journal boundaries."""

from contextlib import nullcontext
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import fixture
import pytest

from unbake import ownership, publish
from unbake import policy as _policy
from unbake.contracts import (
    Config,
    Finding,
    Group,
    Host,
    LayoutMap,
    Member,
    Placement,
    Project,
    Receipt,
    Recipe,
    Refusal,
    Snapshot,
    SourceView,
    UnitSpec,
    Version,
    digest,
)


@pytest.fixture
def lane(tmp_path, monkeypatch):
    project = Project(tmp_path, "fixture", "fixture", "Fixture", ("a", "b"), "a", "gcc-test",
                      {}, {}, {}, {}, 200, {}, "project")
    host = Host(2, 2, 1000, 1000, 1000, tmp_path / "tools", {}, None,
                2, 2, 0.8, 2, ("test", "test@example.invalid"), {}, "host")
    config = Config(project, host, "config")
    unit = UnitSpec("src/f.c", "c", "group", ("f",), "gcc-test", {"add": [], "omit": []})
    group = Group("group", "main", ("f",), "authored", (), False)
    placements = tuple(Placement(v, ".text", 4096, 4104, 0x80000400) for v in ("a", "b"))
    member = Member("f", "function", "asm", "group", placements)
    mapping = LayoutMap(200, {"group": group}, {"f": member}, {}, "map", (), {})
    versions = {v: Version(v, tmp_path / f"{v}.rom", "rom", f"versions/{v}/Game.yaml",
                           f"versions/{v}/symbols.txt", {}, ()) for v in ("a", "b")}
    files = {"src/f.c": b"int f(void) { return 1; }\n", "types.toml": b"old types\n",
             "Makefile": b"same make\n", "README.md": b"old readme\n"}
    snapshot = Snapshot(config, "current-head", mapping, versions, files, "snapshot")
    proposed = replace(snapshot, layout=replace(mapping, units={unit.path: unit}), digest="proposed")
    recipe = Recipe("gcc-test", (), (), (), "recipe")
    proofs = tuple(fixture.proof(unit.path, "f", v, True) for v in ("a", "b"))
    source = tmp_path / "candidate.c"
    source.write_bytes(files[unit.path])
    work = tmp_path / ".unbake" / "work"
    work.mkdir(parents=True)
    dumps = {}

    def dump_map(value):
        data = digest(value).encode()
        dumps[data] = value
        return data

    def overlay(snap, writes):
        merged = {**snap.overlays, **writes}
        mapped = dumps.get(merged.get("layout.toml"), snap.layout)
        return replace(snap, overlays=merged, layout=mapped, digest=digest((snap.digest, writes)))

    monkeypatch.setattr(publish.effort, "stage", Mock(side_effect=lambda name: nullcontext()))
    monkeypatch.setattr(publish.effort, "invocation", Mock(return_value="invocation"))
    monkeypatch.setattr(publish.layout, "capture", Mock(return_value=snapshot))
    monkeypatch.setattr(publish.layout, "overlay", Mock(side_effect=overlay))
    monkeypatch.setattr(publish.layout, "dump_map", Mock(side_effect=dump_map))
    monkeypatch.setattr(publish.layout, "unit_of", Mock(side_effect=lambda snap, name: next(
        (u for u in snap.layout.units.values() if name in u.members), None)))
    monkeypatch.setattr(publish.layout, "unit_options", Mock(return_value=[(unit, {})]))
    monkeypatch.setattr(publish.compare, "bind", Mock(return_value=(unit, proposed)))
    monkeypatch.setattr(publish.compare, "holders", Mock(return_value=("a", "b")))
    monkeypatch.setattr(publish.compare, "gaps", Mock(return_value=()))
    monkeypatch.setattr(publish.compare, "measure", Mock(return_value=proofs))
    monkeypatch.setattr(publish.recipes, "resolve", Mock(return_value=recipe))
    views = {v: SourceView(unit.path, v, v, "source", (), ()) for v in ("a", "b")}
    monkeypatch.setattr(publish.view, "get", Mock(return_value=views["a"]))
    monkeypatch.setattr(publish.view, "all_versions", Mock(return_value=views))
    monkeypatch.setattr(publish.policy, "evaluate", Mock(return_value=()))
    monkeypatch.setattr(publish.policy, "scope", Mock(return_value=((), ())))
    monkeypatch.setattr(publish.headers, "fold", Mock(return_value=({unit.path: files[unit.path]}, ())))
    monkeypatch.setattr(publish.types, "landed", Mock(return_value=files["types.toml"]))
    monkeypatch.setattr(publish.repo, "files", Mock(return_value={"Makefile": files["Makefile"]}))
    monkeypatch.setattr(publish.store, "work", Mock(side_effect=lambda cfg: nullcontext(work)))
    monkeypatch.setattr(publish.pool, "map",
                        Mock(side_effect=lambda cfg, name, fn, items, key=None: [fn(i) for i in items]))
    monkeypatch.setattr(publish.native, "objects", Mock(side_effect=lambda *args: (args[-1] / "out.o", ())))
    monkeypatch.setattr(publish.native, "measure", Mock(side_effect=lambda *args: (proofs[("a", "b").index(args[2])],)))
    monkeypatch.setattr(publish.native, "prove", Mock(side_effect=lambda *args: (proofs[("a", "b").index(args[2])],)))
    monkeypatch.setattr(publish.versions, "undefined", Mock(return_value=()))
    monkeypatch.setattr(publish.versions, "resolve", Mock(return_value=()))
    monkeypatch.setattr(publish.journal, "apply", Mock(return_value="new-commit"))
    submission = fixture.submission(member="f", source="candidate.c", proofs=proofs)
    return {"snapshot": snapshot, "proposed": proposed, "config": config, "unit": unit,
            "proofs": proofs, "source": source, "work": work, "dumps": dumps, "submission": submission}

REAL_SCOPE = _policy.scope


@pytest.fixture(autouse=True)
def _no_ownership(monkeypatch):  # the units here have no source to compile: they own what they list
    derive = Mock(side_effect=lambda snapshot, unit: (snapshot, unit))
    monkeypatch.setattr(ownership, "derive", derive)
    monkeypatch.setattr(ownership, "derive_many", lambda snapshot, units: [derive(snapshot, u) for u in units])
    return derive


def _request(lane, **changes):
    return {"file": "candidate.c", "function": "f", "overrides": {"add": [], "omit": []},
            "note": "", **changes}


def _gap(reason="changed bytes"):
    return Finding("land.not_exact", reason, unit="f", missing=("bytes",), symptoms={"bytes_differ": True})


def test_admission_order(lane):
    tracker = Mock()
    for name, mock in (("bind", publish.compare.bind), ("policy", publish.policy.evaluate),
                       ("recipe", publish.recipes.resolve), ("derive", publish.ownership.derive),
                       ("views", publish.view.all_versions),
                       ("objects", publish.native.objects), ("undefined", publish.versions.undefined),
                       ("resolve", publish.versions.resolve), ("measure", publish.native.measure),
                       ("gaps", publish.compare.gaps)):
        tracker.attach_mock(mock, name)
    overrides = {"toolchain": "alternative", "add": ["-O1"], "omit": []}
    publish.recipes.resolve.return_value = Recipe("alternative", (), (), (), "alternative")
    unit, snap, proofs = publish.admit(lane["snapshot"], _request(lane, overrides=overrides))
    assert [c[0] for c in tracker.mock_calls] == [
        "bind", "policy", "recipe", "derive", "views", "policy", "objects", "objects",
        "undefined", "resolve", "undefined", "resolve", "measure", "measure", "gaps"]
    assert unit.toolchain == "alternative"
    assert unit.options == {"add": ["-O1"], "omit": []}
    assert snap is lane["proposed"] and proofs == lane["proofs"]
    assert publish.policy.evaluate.call_args_list[0].args[3] is None
    assert publish.policy.evaluate.call_args_list[1].args[3].version == "a"
    assert [c.args[-1] for c in publish.native.objects.call_args_list] == [
        lane["work"] / "0", lane["work"] / "1"]
    assert [c.args[-1] for c in publish.native.measure.call_args_list] == [
        lane["work"] / "0", lane["work"] / "1"]
    stages = [c.args[0] for c in publish.effort.stage.call_args_list]
    assert stages == ["publish.admit", *[f"publish.admit.{n}" for n in (1, 3, 4, 5, 6, 7, 8)]]


def test_admission_proves_the_unit_with_the_data_its_source_emits(lane, _no_ownership):
    owned = replace(lane["unit"], members=(*lane["unit"].members, "rodata/f/80000000"))
    derived = replace(lane["proposed"], digest="derived")
    _no_ownership.side_effect = lambda snapshot, unit: (derived, owned)
    unit, snap, _ = publish.admit(lane["snapshot"], _request(lane))
    assert unit is owned and snap is derived
    assert all(c.args[0] is derived and c.args[1] is owned for c in publish.native.measure.call_args_list)


@pytest.mark.parametrize("step", ["owner", "policy3", "policy5", "unresolved", "gaps"])
def test_admission_refuses_at_gate(lane, step):
    snapshot = lane["snapshot"]
    request = _request(lane)
    finding = _gap()
    if step == "owner":
        owner = replace(lane["unit"], path="src/other.c")
        snapshot = replace(snapshot, layout=replace(snapshot.layout, units={owner.path: owner},
                           members={"f": replace(snapshot.layout.members["f"], state="c")}))
    elif step.startswith("policy"):
        publish.policy.scope.side_effect = REAL_SCOPE
        finding = replace(finding, path=lane["unit"].path)
        publish.policy.evaluate.side_effect = [(finding,), ()] if step == "policy3" else [(), (finding,), ()]
    elif step == "unresolved":
        publish.versions.resolve.return_value = (finding,)
    else:
        publish.compare.gaps.return_value = (finding,)
    with pytest.raises(Refusal) as exc:
        publish.admit(snapshot, request)
    assert exc.value.findings[0].key == ("land.request" if step == "owner" else finding.key)
    if step in ("owner", "policy3", "policy5"):
        publish.native.objects.assert_not_called()
    if step != "gaps":
        publish.native.measure.assert_not_called()
    publish.journal.apply.assert_not_called()


def test_admission_allows_nonblocking_debt(lane):
    publish.policy.evaluate.return_value = (Finding("land.request", "existing debt", blocking=False),)
    assert publish.admit(lane["snapshot"], _request(lane))[2] == lane["proofs"]


def test_admission_ignores_findings_the_landed_text_already_carries(lane):
    publish.policy.scope.side_effect = REAL_SCOPE
    old = Finding("source.version-guard", "old guard", path=lane["unit"].path)
    new = Finding("source.volatile-storage", "new qualifier", path=lane["unit"].path)
    publish.policy.evaluate.side_effect = [(old,), (old,), (), (old,), (old,), ()]
    assert publish.admit(lane["snapshot"], _request(lane))[2] == lane["proofs"]
    publish.policy.evaluate.side_effect = [(old, new), (old,)]
    with pytest.raises(Refusal) as exc:
        publish.admit(lane["snapshot"], _request(lane))
    assert [f.reason for f in exc.value.findings] == ["new qualifier"]


def test_plan_removes_fuzzy_row_when_exact(lane):
    snapshot = lane["snapshot"]
    fuzzy_path = "src/fuzzy/f.c"
    snapshot = replace(snapshot, layout=replace(snapshot.layout, fuzzy={
        "f": {"path": fuzzy_path, "scores": {"a": 0.7, "b": 0.6}},
        "other": {"path": "src/fuzzy/other.c", "scores": {"a": 0.5}}}))
    plan, = publish.plans(snapshot, lane["unit"], lane["proposed"])
    assert plan.writes[fuzzy_path] is None
    mapped = lane["dumps"][plan.writes["layout.toml"]]
    assert "f" not in mapped.fuzzy and "other" in mapped.fuzzy
    assert mapped.units[lane["unit"].path] == lane["unit"]


def test_plan_replaces_the_file_of_a_member_that_is_already_landed(lane):
    snapshot, unit = lane["snapshot"], lane["unit"]
    snapshot = replace(snapshot, layout=replace(snapshot.layout, units={unit.path: unit},
                       members={"f": replace(snapshot.layout.members["f"], state="c")}))
    publish.layout.unit_options.side_effect = AssertionError("a landed member needs no new unit option")
    plan, = publish.plans(snapshot, unit, lane["proposed"])
    assert plan.writes[unit.path] == lane["source"].read_bytes()
    publish.headers.fold.assert_not_called()
    assert lane["dumps"][plan.writes["layout.toml"]].units[unit.path] == unit


def test_plan_writes_changed_repo_files_only(lane):
    changed = {"README.md": b"new readme", "versions/a/symbols.ld": b"symbols",
               "versions/a/report.json": b"{}", ".github/workflows/ci.yml": b"ci"}
    publish.repo.files.return_value = {"Makefile": b"same make\n", **changed}
    publish.types.landed.return_value = b"new types"
    plan, = publish.plans(lane["snapshot"], lane["unit"], lane["proposed"])
    assert "Makefile" not in plan.writes
    assert all(plan.writes[p] == data for p, data in changed.items())
    assert plan.writes["types.toml"] == b"new types"
    assert publish.repo.files.call_args.args[0].read("types.toml") == b"new types"
    assert publish.types.landed.call_args.args[1] == lane["unit"]
    assert plan.affected == ("src/f.c",) and plan.operation == "publish"
    assert plan.digest == digest((plan.operation, plan.base, plan.writes, plan.affected,
                                  plan.blocking, plan.debt, plan.message))


def test_plan_appends_and_reproves_every_unit_that_includes_the_edited_header_through_headers(lane):
    snapshot, unit = lane["snapshot"], lane["unit"]
    consumer = replace(unit, path="src/consumer.c", members=("consumer",))
    outsider = replace(consumer, path="src/outsider.c", group="other")
    bystander = replace(consumer, path="src/bystander.c", members=("bystander",))
    header = "include/main/group.h"
    old = b'#include "old.h"\nint old(void);\n'
    units = {u.path: u for u in (unit, consumer, outsider, bystander)}
    snapshot = replace(snapshot, layout=replace(snapshot.layout, units=units),
                       overlays={**snapshot.overlays, unit.path: old, header: b"old header",
                                 consumer.path: b'#include "main/group.h"\n',
                                 "include/mid.h": b'#include "main/group.h"\n',
                                 outsider.path: b'#include "mid.h"\n',
                                 bystander.path: b'#include "other.h"\n'})
    publish.headers.sources.return_value = [header, "include/mid.h"]
    folded = b'#include "old.h"\n#include "new.h"\n#include "new.h"\nint f(void);\n'
    publish.headers.fold.return_value = ({unit.path: folded, header: b"new header"}, ())
    debt = Finding("land.request", "existing debt", blocking=False)
    publish.policy.scope.return_value = ((), (debt,))
    plan, = publish.plans(snapshot, unit, lane["proposed"])
    assert plan.writes[unit.path] == b'#include "old.h"\n#include "new.h"\nint old(void);\n\nint f(void);\n'
    assert plan.affected == tuple(sorted((unit.path, consumer.path, outsider.path)))  # not the bystander
    assert plan.debt == (debt,)
    assert publish.policy.scope.call_args.args[2] == plan.writes


def test_consumers_read_nothing_without_a_header_edit_and_reprove_includers_of_a_deleted_header(monkeypatch):
    reads = []
    files = {"src/a.c": b'#include "gone.h"\n', "src/b.c": b'#include "kept.h"\n'}

    def peek(path):
        reads.append(path)
        return files.get(path)

    snapshot = SimpleNamespace(peek=peek, layout=SimpleNamespace(units=dict.fromkeys(files)))
    monkeypatch.setattr(publish.headers, "sources", lambda _: ["include/kept.h"])
    assert publish._consumers(snapshot, []) == () and reads == []
    assert publish._consumers(snapshot, ["include/gone.h"]) == ("src/a.c",)
    assert sorted(reads) == ["include/gone.h", "include/kept.h", "src/a.c", "src/b.c"]  # each file once


@pytest.mark.parametrize("reason", ["fold", "scope", "no_options"])
def test_plan_refusals(lane, reason):
    finding = Finding("land.request", "cannot land")
    if reason == "fold":
        publish.headers.fold.return_value = ({}, (finding,))
    elif reason == "scope":
        publish.policy.scope.return_value = ((finding,), ())
    else:
        publish.layout.unit_options.return_value = []
    with pytest.raises(Refusal) as exc:
        list(publish.plans(lane["snapshot"], lane["unit"], lane["proposed"]))
    assert exc.value.findings[0].key == "land.request"
    publish.journal.apply.assert_not_called()


def test_plan_tries_next_layout_option(lane):
    second = replace(lane["unit"], path="src/second.c")
    publish.layout.unit_options.return_value = [(lane["unit"], {}), (second, {})]
    publish.policy.scope.side_effect = [((_gap(),), ()), ((), ())]
    plan, = publish.plans(lane["snapshot"], lane["unit"], lane["proposed"])
    assert plan.affected == (second.path,)
    assert lane["dumps"][plan.writes["layout.toml"]].units[second.path] == second


def test_plan_falls_through_an_option_that_refuses_outright(lane):
    second = replace(lane["unit"], path="src/second.c")
    publish.layout.unit_options.return_value = [(lane["unit"], {}), (second, {})]
    publish.types.landed.side_effect = [Refusal(Finding("headers.parse", "no parse")), b"types"]
    plan, = publish.plans(lane["snapshot"], lane["unit"], lane["proposed"])
    assert plan.affected == (second.path,)


def test_plan_refuses_with_the_last_refusal_when_every_option_refuses(lane):
    publish.types.landed.side_effect = Refusal(Finding("headers.parse", "no parse"))
    with pytest.raises(Refusal) as exc:
        list(publish.plans(lane["snapshot"], lane["unit"], lane["proposed"]))
    assert exc.value.findings[0].key == "headers.parse"


def test_land_publish_commits_once(lane):
    receipt = publish.land(lane["config"], lane["submission"])
    plan = publish.journal.apply.call_args.args[1]
    publish.journal.apply.assert_called_once_with(lane["config"], plan, "current-head")
    assert receipt == Receipt("publish", "new-commit", plan.digest, lane["proofs"], 0, "invocation")
    publish.compare.bind.assert_called_once_with(lane["snapshot"], lane["source"], "f")
    assert publish.native.prove.call_count == 2
    assert [c.args[-1] for c in publish.native.prove.call_args_list] == [lane["work"] / "0", lane["work"] / "1"]


@pytest.mark.parametrize("same,decompiled", [(True, True), (False, True), (True, False)])
def test_land_duplicate_refuses(lane, same, decompiled):
    snapshot, unit = lane["snapshot"], lane["unit"]
    unit = replace(unit, kind="c" if decompiled else "asm")
    snapshot = replace(snapshot, layout=replace(snapshot.layout, units={unit.path: unit}))
    publish.layout.capture.return_value = snapshot
    if not same:
        lane["source"].write_bytes(b"changed candidate")
    if same and decompiled:
        with pytest.raises(Refusal) as exc:
            publish.land(lane["config"], lane["submission"])
        assert exc.value.findings[0].key == "land.duplicate"
        assert exc.value.findings[0].unit == "f"
        publish.compare.bind.assert_not_called()
        publish.journal.apply.assert_not_called()
    else:
        assert publish.land(lane["config"], lane["submission"]).operation == "publish"


@pytest.mark.parametrize("same", [True, False])
def test_land_replaces_the_untyped_source_of_a_landed_data_member_with_a_typed_one(lane, same):
    snapshot, unit = lane["snapshot"], lane["unit"]
    unit = replace(unit, kind="c")
    landed = replace(snapshot.layout.members["f"], kind="rodata", state=".rodata")  # landed data
    members = {**snapshot.layout.members, "f": landed}
    snapshot = replace(snapshot, layout=replace(snapshot.layout, units={unit.path: unit}, members=members))
    publish.layout.capture.return_value = snapshot
    if not same:
        lane["source"].write_bytes(b"const unsigned int f = 1;")  # the typed text of the same bytes
    publish.layout.unit_options.side_effect = AssertionError("a landed member needs no new unit option")
    if same:
        with pytest.raises(Refusal) as exc:
            publish.land(lane["config"], lane["submission"])
        assert exc.value.findings[0].key == "land.duplicate"
        publish.journal.apply.assert_not_called()
    else:
        assert publish.land(lane["config"], lane["submission"]).operation == "publish"
        publish.compare.bind.assert_called_once_with(snapshot, lane["source"], "f")
def test_land_not_exact_after_head_moved(lane, monkeypatch):
    snapshot = lane["snapshot"]
    plan = publish._plan("publish", snapshot, {"src/f.c": b"candidate"}, ("src/f.c",), (), "publish f")
    monkeypatch.setattr(publish, "admit", Mock(return_value=(lane["unit"], lane["proposed"], lane["proofs"])))
    monkeypatch.setattr(publish, "plans", Mock(return_value=[plan, replace(plan, message="alternate")]))
    stale = fixture.proof("src/f.c", "f", "a", False, ("bytes",))
    first, last = _gap("first option"), _gap("last option")
    monkeypatch.setattr(publish, "_proofs", Mock(side_effect=[((stale,), (first,)), ((stale,), (last,))]))
    with pytest.raises(Refusal) as exc:
        publish.land(lane["config"], replace(lane["submission"], base="old-head"))
    assert exc.value.findings == (last,)
    assert publish._proofs.call_count == 2
    publish.journal.apply.assert_not_called()


def test_land_first_exact_plan_wins(lane, monkeypatch):
    snapshot = lane["snapshot"]
    plans = [publish._plan("publish", snapshot, {"src/f.c": str(i).encode()}, ("src/f.c",), (), str(i))
             for i in range(3)]
    monkeypatch.setattr(publish, "admit", Mock(return_value=(lane["unit"], lane["proposed"], lane["proofs"])))
    monkeypatch.setattr(publish, "plans", Mock(return_value=plans))
    monkeypatch.setattr(publish, "_proofs", Mock(side_effect=[((), (_gap(),)), (lane["proofs"], ())]))
    receipt = publish.land(lane["config"], lane["submission"])
    assert receipt.plan == plans[1].digest
    assert publish._proofs.call_count == 2
    publish.journal.apply.assert_called_once_with(lane["config"], plans[1], snapshot.commit)


@pytest.mark.parametrize("exact_at_head, lands", [(False, True), (True, False)])
def test_land_a_consumers_gap_blocks_only_when_head_proves_it_exact(lane, monkeypatch, exact_at_head, lands):
    snapshot = lane["snapshot"]
    plan = publish._plan("publish", snapshot, {"src/f.c": b"0"}, ("src/f.c", "src/g.c"), (), "0")
    error = "src/g.c:3: conflicting types for 'x'"
    consumer = Finding("land.not_exact", "g differs", unit="g", versions=("a",), missing=(error,))
    monkeypatch.setattr(publish, "admit", Mock(return_value=(lane["unit"], lane["proposed"], lane["proofs"])))
    monkeypatch.setattr(publish, "plans", Mock(return_value=[plan]))
    unit_of = publish.layout.unit_of.side_effect
    g = replace(lane["unit"], path="src/g.c", members=("g",))
    pick = Mock(side_effect=lambda snap, name: g if name == "g" else unit_of(snap, name))
    monkeypatch.setattr(publish.layout, "unit_of", pick)
    head = (fixture.proof("src/g.c", "g", "a", exact_at_head, () if exact_at_head else ("bytes",)),)
    monkeypatch.setattr(publish, "_proofs", Mock(side_effect=[(lane["proofs"], (consumer,)), (head, ())]))
    if lands:
        assert publish.land(lane["config"], lane["submission"]).plan == plan.digest
    else:
        with pytest.raises(Refusal) as exc:
            publish.land(lane["config"], lane["submission"])
        finding, = exc.value.findings
        assert (finding.key, finding.path, finding.unit, finding.missing) == (
            "land.breaks_dependent", "src/g.c", "g", (error,))
        assert finding.reason == f"{lane['unit'].path} breaks src/g.c: {error}" and "src/g.c" in finding.action
    assert publish._proofs.call_args_list[1].args[1] == ["src/g.c"]


def _fuzzy_proofs(lane, scores, symptoms=None, missing=("bytes",)):
    return tuple(fixture.proof(lane["unit"].path, "f", v, False, missing, score, symptoms)
                 for v, score in zip(("a", "b"), scores, strict=True))


def test_land_fuzzy_writes_row_and_file(lane):
    proofs = _fuzzy_proofs(lane, (0.8, 0.6))
    publish.compare.measure.return_value = proofs
    publish.repo.files.return_value = {"Makefile": b"same make\n", "README.md": b"fuzzy readme"}
    receipt = publish.land(lane["config"], replace(lane["submission"], operation="fuzzy"))
    plan = publish.journal.apply.call_args.args[1]
    assert plan.operation == "fuzzy" and plan.affected == () and plan.message == "fuzzy f 60.0%"
    assert plan.writes["src/fuzzy/f.c"] == lane["source"].read_bytes()
    mapped = lane["dumps"][plan.writes["layout.toml"]]
    assert mapped.fuzzy["f"] == {"path": "src/fuzzy/f.c", "scores": {"a": 0.8, "b": 0.6}}
    assert "Makefile" not in plan.writes and plan.writes["README.md"] == b"fuzzy readme"
    assert receipt == Receipt("fuzzy", "new-commit", plan.digest, proofs, 0, "invocation")
    publish.compare.measure.assert_called_once_with(lane["proposed"], lane["unit"],
                                                    lane["submission"].overrides, None)
    assert publish.policy.evaluate.call_count == 2
    publish.native.objects.assert_not_called()
    publish.journal.apply.assert_called_once()


@pytest.mark.parametrize("prior,scores,allowed", [
    (None, (0.5, 0.0), False), ({"a": 0.8, "b": 0.5}, (0.9, 0.51), False),
    ({"a": 0.8, "b": 0.5}, (0.9, 0.55), True), ({"a": 0.8, "b": 0.5}, (0.9, 0.49), False),
])
def test_land_fuzzy_no_gain(lane, prior, scores, allowed, monkeypatch):
    real = publish.configuration.load_resource
    monkeypatch.setattr(publish.configuration, "load_resource", lambda name: (
        {"fuzzy": {"min_gain": 0.05}} if name == "flow.toml" else real(name)))
    snapshot = lane["snapshot"]
    if prior is not None:
        snapshot = replace(snapshot, layout=replace(snapshot.layout, fuzzy={
            "f": {"path": "src/fuzzy/f.c", "scores": prior}}))
    publish.layout.capture.return_value = snapshot
    publish.compare.measure.return_value = _fuzzy_proofs(lane, scores)
    submission = replace(lane["submission"], operation="fuzzy")
    if allowed:
        assert publish.land(lane["config"], submission).operation == "fuzzy"
    else:
        with pytest.raises(Refusal) as exc:
            publish.land(lane["config"], submission)
        assert exc.value.findings[0].key == "fuzzy.no_gain"
        publish.journal.apply.assert_not_called()


@pytest.mark.parametrize("symptom", ["compile_failed", "unresolved", "no_measurement"])
def test_land_fuzzy_compile_failed_refuses(lane, symptom):
    proofs = _fuzzy_proofs(lane, (0.9, 0.8), {symptom: True}, (symptom,))
    publish.compare.measure.return_value = proofs
    with pytest.raises(Refusal) as exc:
        publish.land(lane["config"], replace(lane["submission"], operation="fuzzy"))
    finding, = exc.value.findings
    assert finding.key == "land.not_exact" and finding.unit == "f"
    assert set(finding.missing) == {symptom} and finding.symptoms[symptom]
    publish.journal.apply.assert_not_called()


def test_a_later_layout_option_is_planned_only_when_the_first_does_not_prove(lane, monkeypatch):
    first, second = Mock(name="first"), Mock(name="second")
    monkeypatch.setattr(publish, "_option_plan", Mock(side_effect=[first, second]))
    monkeypatch.setattr(publish, "Plan", type(first))
    monkeypatch.setattr(publish.layout, "unit_options", Mock(return_value=[(lane["unit"], {}), (lane["unit"], {})]))
    monkeypatch.setattr(publish.headers, "fold", Mock(return_value=({lane["unit"].path: b"x"}, ())))
    monkeypatch.setattr(publish.view, "get", Mock())
    monkeypatch.setattr(publish.compare, "holders", Mock(return_value=("a",)))
    taken = publish.plans(lane["snapshot"], lane["unit"], lane["proposed"])
    assert next(taken) is first and publish._option_plan.call_count == 1
