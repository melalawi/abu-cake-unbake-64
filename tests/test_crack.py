"""Cracking ladder unit tests: no native tools, repositories, ROMs or network."""

import json
from contextlib import nullcontext
from dataclasses import asdict, replace
from datetime import datetime
from unittest.mock import Mock

import fixture
import pytest

from unbake import config as configuration
from unbake import crack
from unbake.contracts import (
    Config,
    Group,
    Host,
    LayoutMap,
    Member,
    Placement,
    Project,
    Recipe,
    Refusal,
    Snapshot,
    UnitSpec,
    Version,
)

MEMBER = "func_80000400"
SOURCE = b"int func_80000400(void) { return 0; }\n"
RECIPE = Recipe("gcc-test", ("-DTEST",), ("-O2",), (), "1" * 64)
EMPTY = {"add": [], "omit": []}
DRAIN = {"running": False, "landed": [], "refused": []}


@pytest.fixture
def lane(tmp_path, monkeypatch):
    project = Project(tmp_path, "fixture", "fixture", "Fixture", ("a", "b"), "a", "gcc-test",
                      {k: [] for k in ("cppflags", "cflags", "asflags", "gnu_asflags")},
                      {}, {"a": (), "b": ()}, {}, 200, {}, "0" * 64)
    host = Host(2, 2, 1 << 30, 1 << 30, 1 << 30, tmp_path / "tools",
                {}, None, 2, 2, 0.8, 2, ("test", "test@example.invalid"), {}, "0" * 64)
    config = Config(project, host, "0" * 64)
    places = tuple(Placement(v, ".text", 0x1000, 0x1008, 0x80000400) for v in ("a", "b"))
    member = Member(MEMBER, "function", "asm", "main", places)
    group = Group("main", "main", (MEMBER,), "authored", (), "unknown", False)
    mapping = LayoutMap(200, {"main": group}, {MEMBER: member}, {}, "0" * 64, (), {})
    versions = {v: Version(v, tmp_path / f"{v}.z64", "0" * 64, "", "", {}, ()) for v in ("a", "b")}
    snapshot = Snapshot(config, "0" * 40, mapping, versions, {}, "0" * 64)
    rows = []
    monkeypatch.setattr(crack.effort, "stage", Mock(side_effect=lambda name: nullcontext()))
    monkeypatch.setattr(crack.effort, "invocation", lambda: "test-invocation")
    monkeypatch.setattr(crack.store, "rows", Mock(side_effect=lambda cfg, stream: list(rows)))
    monkeypatch.setattr(crack.store, "append", Mock(side_effect=lambda cfg, stream, row: rows.append(row)))
    monkeypatch.setattr(crack.draft, "hints", Mock(return_value=([], [])))
    monkeypatch.setattr(crack.draft, "split_slot", Mock(return_value=None))
    monkeypatch.setattr(crack.process, "run", Mock(side_effect=AssertionError("unmocked native invocation")))
    monkeypatch.setattr(crack.process, "git", Mock(side_effect=AssertionError("git forbidden")))
    return snapshot, rows


def _proof(score, version="a", symptoms=None, member=MEMBER):
    return replace(fixture.proof(f".unbake/work/{MEMBER}.c", member, version, score == 1,
                                 missing=() if score == 1 else ("bytes",), score=score, symptoms=symptoms),
                   recipe=RECIPE.digest)


def _prior(rows, **fields):
    rows.append(asdict(fixture.attempt(**{"member": MEMBER, "step": "options", "recipe": RECIPE.digest, **fields})))


def _candidate(snapshot):
    path = snapshot.config.project.root / f".unbake/work/{MEMBER}.c"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(SOURCE)
    return path


def _measurement(snapshot, monkeypatch, proofs=None):
    unit = UnitSpec(f".unbake/work/{MEMBER}.c", "c", "main", (MEMBER,), "gcc-test", {})
    monkeypatch.setattr(crack.compare, "bind", Mock(return_value=(unit, snapshot)))
    monkeypatch.setattr(crack.compare, "measure", Mock(return_value=proofs or (_proof(0.5), _proof(0.4, "b"))))
    monkeypatch.setattr(crack.recipes, "resolve", Mock(return_value=RECIPE))
    monkeypatch.setattr(crack.versions, "asm_path", lambda cfg, version, member:
                        cfg.project.root / f"asm/{version}/{member}.s")
    asm = crack.versions.asm_path(snapshot.config, "a", MEMBER)
    asm.parent.mkdir(parents=True)
    asm.write_text("jal callee\nlui $t0, %hi(global)\naddiu $t0, $t0, %lo(global)\njal callee\n")
    monkeypatch.setattr(crack.versions, "rom_bytes", Mock(return_value=b"target!!"))
    monkeypatch.setattr(crack.store, "cached", Mock(return_value=b"built!!!extra"))
    monkeypatch.setattr(crack.symptoms, "diff", Mock(return_value="measured diff"))
    monkeypatch.setattr(crack.types, "signature", Mock(return_value={MEMBER: "int f(void)"}))
    monkeypatch.setattr(crack.types, "declarations", Mock(return_value={"callee": "void callee(void);",
                                                                       "global": "extern int global;"}))
    return unit


def _runner(snapshot, monkeypatch, scores, output=None):
    unit = _measurement(snapshot, monkeypatch)
    batches = [(_proof(score), _proof(score, "b")) for score in scores]
    crack.compare.measure.side_effect = batches
    monkeypatch.setattr(crack.layout, "capture", Mock(return_value=snapshot))
    monkeypatch.setattr(crack.layout, "unit_of", Mock(return_value=None))
    monkeypatch.setattr(crack.layout, "overlay", Mock(side_effect=lambda snap, writes:
                        replace(snap, overlays={**snap.overlays, **writes})))

    def create(snap, member, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(SOURCE)
        return {}

    monkeypatch.setattr(crack.draft, "create", Mock(side_effect=create))
    proposals = [replace(RECIPE, cflags=("-O1",), digest="2" * 64),
                 replace(RECIPE, cppflags=("-DOTHER",), cflags=("-O1",), digest="3" * 64)]
    monkeypatch.setattr(crack.recipes, "proposals", Mock(return_value=proposals))
    work = snapshot.config.project.root / "permuter-work"
    work.mkdir()
    monkeypatch.setattr(crack.store, "work", lambda cfg: nullcontext(work))
    assembler = Mock()
    assembler.assemble.return_value = fixture.native_result()
    monkeypatch.setattr(crack.adapters, "assembler", Mock(return_value=assembler))
    monkeypatch.setattr(crack.adapters, "script", Mock(return_value="#!/bin/sh\nexit 0\n"))
    monkeypatch.setattr(crack.process, "tool", Mock(return_value=work / "permuter"))

    def permute(*args, **kwargs):
        if output is not None:
            path = work / "output-0-1/source.c"
            path.parent.mkdir()
            path.write_bytes(output)
        return fixture.native_result()

    monkeypatch.setattr(crack.process, "run", Mock(side_effect=permute))
    monkeypatch.setattr(crack.land, "submit", Mock(return_value=fixture.submission(origin="crack")))
    monkeypatch.setattr(crack.land, "drain", Mock(return_value=DRAIN))
    return unit


def test_state_derivation(lane):
    snapshot, _ = lane
    assert crack.state(snapshot, MEMBER) == "open"
    root = snapshot.config.project.root / ".unbake"
    attempts = root / f"attempts/{MEMBER}.jsonl"
    attempts.parent.mkdir(parents=True)
    attempts.touch()
    assert crack.state(snapshot, MEMBER) == "tool"
    packet = root / f"packets/{MEMBER}.json"
    packet.parent.mkdir()
    packet.touch()
    assert crack.state(snapshot, MEMBER) == "creative"
    fuzzy = replace(snapshot, layout=replace(snapshot.layout, fuzzy={MEMBER: {}}))
    assert crack.state(fuzzy, MEMBER) == "fuzzy"


@pytest.mark.parametrize("scores,facts,before,outcome", [
    ((1, 1), {}, 0.8, "exact"), ((1, 0.6), {}, 0.5, "better"),
    ((0.6, 0.4), {"bytes_differ": True}, 0.4, "same"),
    ((0.6, 0.3), {"bytes_differ": True}, 0.4, "worse"),
    ((0.2, 0), {"compile_failed": True}, 0.4, "failed"),
    ((0.2, 0), {"no_measurement": True}, 0.0, "failed"),
    ((0.2, 0), {"unresolved": True}, 0.0, "failed"),
    ((0.2, 0), {}, 0.0, "same"), ((), {}, 0.0, "same"),
])
def test_feedback_outcomes_and_plateau(lane, scores, facts, before, outcome):
    snapshot, rows = lane
    _prior(rows, score=before, outcome="same")
    _prior(rows, score=before, outcome="worse")
    proofs = [_proof(score, v, {} if score == 1 else facts) for score, v in zip(scores, ("a", "b"), strict=False)]
    proofs.append(_proof(0, member="unrelated", symptoms={"compile_failed": True}))
    attempt, feedback = crack.feedback(snapshot, MEMBER, "options", proofs, "note")
    assert attempt.outcome == feedback["outcome"] == outcome
    assert attempt.score == feedback["score_after"] == min(scores, default=0)
    assert attempt.best_before == feedback["score_before"] == before
    assert attempt.symptoms == {**facts, "plateau_probes": 2 + int(outcome in ("same", "worse"))}
    assert feedback["label"] == ("CRACKED" if outcome == "exact" else "")
    if len(proofs) > 1:
        assert proofs[0].symptoms == ({} if scores[0] == 1 else facts)


@pytest.mark.parametrize("step,outcome,expected", [("options", "same", 2), ("draft", "same", 1),
                                                   ("options", "better", 1)])
def test_feedback_plateau_stops_at_intervening_attempt(lane, step, outcome, expected):
    snapshot, rows = lane
    _prior(rows, score=0.4, outcome="same")
    _prior(rows, score=0.4, outcome="same", step="draft")
    _prior(rows, score=0.4, outcome="worse")
    score = 0.5 if outcome == "better" else 0.4
    attempt, _ = crack.feedback(snapshot, MEMBER, step, [_proof(score)], "")
    assert attempt.symptoms["plateau_probes"] == expected


@pytest.mark.parametrize("tried,subsystem,expected", [
    (("first",), "unknown", "second technique"), ((), "unknown", "first technique"),
    (("first", "second"), "math", "idiom"), (("first", "second"), "unknown", "creative"),
])
def test_feedback_next_prefers_untried_hint(lane, monkeypatch, tried, subsystem, expected):
    snapshot, rows = lane
    group = replace(snapshot.layout.groups["main"], subsystem=subsystem)
    snapshot = replace(snapshot, layout=replace(snapshot.layout, groups={"main": group}))
    hints = [{"id": name, "technique": f"{name} technique", "example": "", "because": {"bytes_differ": True}}
             for name in ("first", "second")]
    monkeypatch.setattr(crack.draft, "hints", Mock(return_value=(hints, [])))
    _prior(rows, hints=tried)
    _, feedback = crack.feedback(snapshot, MEMBER, "draft", [_proof(0.5)], "")
    row = next(r for r in configuration.load_resource("subsystems.toml")["subsystem"] if r["id"] == subsystem)
    if expected == "idiom":
        expected = row["idioms"][0]
    elif expected == "creative":
        expected = next(r["help"] for r in configuration.load_resource("flow.toml")["ladder"] if r["id"] == "creative")
    assert feedback["next"] == expected
    assert feedback["hints"] == hints


def test_feedback_records_attempt(lane):
    snapshot, rows = lane
    proofs = [_proof(0.8), _proof(0.4, "b")]
    attempt, _ = crack.feedback(snapshot, MEMBER, "draft", proofs, "retain this technique")
    assert rows == [json_roundtrip(asdict(attempt))]
    crack.store.append.assert_called_once_with(snapshot.config, f"attempts/{MEMBER}", rows[0])
    assert attempt.source_sha256 == proofs[0].source_sha256
    assert attempt.recipe == proofs[0].recipe
    assert attempt.note == "retain this technique"
    assert attempt.invocation == "test-invocation"
    assert datetime.fromisoformat(attempt.time).utcoffset().total_seconds() == 0
    assert crack.history(snapshot.config, MEMBER) == [attempt]
    assert isinstance(crack.history(snapshot.config, MEMBER)[0].hints, tuple)
    configuration.validate("attempt", rows[0], "attempt.json")


def json_roundtrip(body):
    import json

    return json.loads(json.dumps(body))


@pytest.mark.parametrize("exact_step,scores", [("draft", [1]), ("options", [0.4, 1]),
                                               ("permuter", [0.4, 0.5, 0.3, 1])])
def test_run_stops_at_exact_and_submits(lane, monkeypatch, exact_step, scores):
    snapshot, _ = lane
    output = b"int func_80000400(void) { return 1; }\n"
    unit = _runner(snapshot, monkeypatch, scores, output)
    monkeypatch.setattr(crack, "packet", Mock(side_effect=AssertionError("exact must not write packet")))
    result = crack.run(snapshot.config, {"item": MEMBER, "seconds": 3})
    assert result["state"] == "exact"
    assert result["label"] == "CRACKED"
    assert result["best"] == 1
    assert result["packet"] is None
    assert result["steps"][-1] == {"step": exact_step, "score": 1, "outcome": "exact"}
    assert crack.compare.measure.call_count == len(scores)
    expected_overrides = EMPTY if exact_step == "draft" else {"add": ["-O1"], "omit": ["-O2"]}
    source = output if exact_step == "permuter" else SOURCE
    request = {"file": result["candidate"], "function": MEMBER, "overrides": expected_overrides, "note": ""}
    crack.land.submit.assert_called_once_with(snapshot.config, request, unit,
                                             crack.land.submit.call_args.args[3], source, "crack")
    assert all(p.exact for p in crack.land.submit.call_args.args[3])
    crack.land.drain.assert_called_once_with(snapshot.config)
    assert result["submitted"] == crack.land.submit.return_value.id
    assert result["drain"] == DRAIN
    if exact_step != "permuter":
        crack.process.run.assert_not_called()
    else:
        assert crack.process.run.call_args.kwargs["timeout"] == 3.0
        assert (snapshot.config.project.root / unit.path).read_bytes() == output
    stages = [call.args[0] for call in crack.effort.stage.call_args_list]
    assert "crack.run" in stages and f"crack.{exact_step}" in stages
    configuration.validate("result.crack", result, "crack.json")


@pytest.mark.parametrize("output,last_score", [(None, None), (b"better candidate", 0.7), (b"worse candidate", 0.2)])
def test_run_writes_packet_when_not_exact(lane, monkeypatch, output, last_score):
    snapshot, _ = lane
    scores = [0.4, 0.5, 0.3] + ([] if last_score is None else [last_score]) + [0.5]
    _runner(snapshot, monkeypatch, scores, output)
    result = crack.run(snapshot.config, {"item": MEMBER, "seconds": 2})
    assert result["state"] == "creative" and result["label"] == "NEEDS CREATIVE"
    assert result["best"] == max(0.5, last_score or 0)
    path = snapshot.config.project.root / f".unbake/packets/{MEMBER}.json"
    assert result["packet"] == str(path)
    import json

    packet = json.loads(path.read_text())
    configuration.validate("packet", packet, str(path))
    assert packet["attempts"]["count"] == len(scores) - 1
    assert packet["attempts"]["steps"] == (["draft", "options"] if output is None else
                                            ["draft", "options", "permuter"])
    assert (snapshot.config.project.root / f".unbake/work/{MEMBER}.c").read_bytes() == (
        output if last_score is not None and last_score > 0.5 else SOURCE)
    assert crack.land.submit.call_args.args[1]["overrides"] == {"add": ["-O1"], "omit": ["-O2"]}
    assert min(p.score for p in crack.land.submit.call_args.args[3]) == result["best"]
    configuration.validate("result.crack", result, "crack.json")


def test_packet_siblings_order(lane, monkeypatch):
    snapshot, rows = lane
    _candidate(snapshot)
    _measurement(snapshot, monkeypatch)
    members, units = dict(snapshot.layout.members), {}
    groups = dict(snapshot.layout.groups)
    groups["other"] = replace(groups["main"], name="other")
    groups["math"] = replace(groups["main"], name="math", subsystem="math")
    siblings = [("same_far", "main", 100, "c"), ("same_near", "main", 50, "c"),
                ("other_nearest", "other", 1, "c"), ("other_far", "other", 200, "c"),
                ("wrong_subsystem", "math", 0, "c"), ("assembly", "main", 0, "asm")]
    siblings += [(f"extra{i}", "other", 300 + i, "c") for i in range(8)]
    for name, group, distance, kind in reversed(siblings):
        places = (Placement("a", ".text", 0x2000, 0x2008, 0x80000400 + distance),)
        members[name] = Member(name, "function", kind, group, places)
        units[name] = UnitSpec(f"src/{name}.c", kind, group, (name,), "gcc-test", {})
    snapshot = replace(snapshot, layout=replace(snapshot.layout, members=members, groups=groups, units=units))
    crack.compare.bind.return_value = (crack.compare.bind.return_value[0], snapshot)
    _prior(rows, score=0.5, outcome="same", symptoms={"plateau_probes": 3})
    path = crack.packet(snapshot, MEMBER)
    import json

    packet = json.loads(path.read_text())
    assert [row["member"] for row in packet["siblings"]] == [
        "same_near", "same_far", "other_nearest", "other_far", "extra0", "extra1"]
    assert all(row["path"] == f"src/{row['member']}.c" for row in packet["siblings"])
    assert packet["versions"] == ["a", "b"]
    assert packet["target"]["a"] == {"asm": f"asm/a/{MEMBER}.s", "size": 8, "vram": 0x80000400}
    assert packet["recipe"] == {"cppflags": ["-DTEST"], "cflags": ["-O2"]}
    assert packet["best"]["score"] == {"a": 0.5, "b": 0.4}
    assert packet["diff"] == "measured diff"
    crack.symptoms.diff.assert_called_once_with(b"built!!!", b"target!!", 0x80000400)
    crack.versions.rom_bytes.assert_called_once_with(snapshot.versions["b"], 0x1000, 0x1008)
    crack.types.declarations.assert_called_once_with(snapshot, ["callee", "global"])
    assert packet["types"] == {"signature": "int f(void)",
                                "declarations": ["void callee(void);", "extern int global;"]}
    assert packet["attempts"] == {"count": 1, "best": 0.5, "steps": ["options"], "plateau_probes": 3}
    assert packet["commands"] == {verb: f"unbake {verb} .unbake/work/{MEMBER}.c --function {MEMBER}"
                                   for verb in ("compare", "submit")}
    assert not path.with_name(path.name + ".part").exists()
    configuration.validate("packet", packet, str(path))


def test_packet_requires_candidate(lane, monkeypatch):
    snapshot, _ = lane
    bind = Mock(side_effect=AssertionError("candidate must be checked before binding"))
    monkeypatch.setattr(crack.compare, "bind", bind)
    with pytest.raises(Refusal) as caught:
        crack.packet(snapshot, MEMBER)
    finding, = caught.value.findings
    assert finding.key == "land.request"
    assert finding.reason == f"{MEMBER} has no candidate"
    assert finding.action == f"unbake crack {MEMBER} --seconds N"
    bind.assert_not_called()


@pytest.mark.parametrize("reason", ["seconds", "unknown", "matched"])
def test_run_refuses_invalid_request(lane, monkeypatch, reason):
    snapshot, _ = lane
    monkeypatch.setattr(crack.layout, "capture", Mock(return_value=snapshot))
    unit = UnitSpec("src/landed.c", "c", "main", (MEMBER,), "gcc-test", {})
    monkeypatch.setattr(crack.layout, "unit_of", Mock(return_value=unit if reason == "matched" else None))
    monkeypatch.setattr(crack.draft, "create", Mock(side_effect=AssertionError("invalid request must not draft")))
    with pytest.raises(Refusal) as caught:
        crack.run(snapshot.config, {"item": "unknown" if reason == "unknown" else MEMBER,
                                    "seconds": 0 if reason == "seconds" else 1})
    assert caught.value.findings[0].key == "land.request"
    crack.draft.create.assert_not_called()


def test_run_on_a_withheld_unit_names_the_versions_and_the_compare_command(lane, monkeypatch):
    snapshot, _ = lane
    monkeypatch.setattr(crack.layout, "capture", Mock(return_value=snapshot))
    unit = UnitSpec("src/landed.c", "c", "main", (MEMBER,), "gcc-test", {}, ("b",))
    monkeypatch.setattr(crack.layout, "unit_of", Mock(return_value=unit))
    with pytest.raises(Refusal) as caught:
        crack.run(snapshot.config, {"item": MEMBER, "seconds": 1})
    finding = caught.value.findings[0]
    assert finding.versions == ("b",) and "withheld in b" in finding.reason
    assert finding.action == f"unbake compare src/landed.c --function {MEMBER}"


def test_result_validates(lane, monkeypatch):
    snapshot, _ = lane
    _candidate(snapshot)
    _runner(snapshot, monkeypatch, [0, 0, 0, 0])
    result = crack.run(snapshot.config, {"item": MEMBER, "seconds": 1})
    assert result["submitted"] is None and result["drain"] is None
    crack.land.submit.assert_not_called()
    crack.land.drain.assert_not_called()
    crack.draft.create.assert_not_called()
    configuration.validate("result.crack", result, "crack.json")


def test_a_data_member_with_slashes_keeps_its_attempts_in_one_stream(monkeypatch):
    rows = Mock(return_value=[])
    monkeypatch.setattr(crack.store, "rows", rows)
    assert crack.history(Mock(), "rodata/func_f/800C0000") == []
    rows.assert_called_once()
    assert rows.call_args.args[1] == "attempts/rodata.func_f.800C0000"
    assert crack.store._STREAM.match(rows.call_args.args[1])


def test_a_member_name_too_long_for_a_file_keeps_a_short_unique_stem():
    long, other = "rodata/x/" + "_unclaimed_CE0F0" * 30, "rodata/x/" + "_unclaimed_CE0F4" * 30
    first = crack.store.stem(long)
    assert len(first) <= 120 and first != crack.store.stem(other) and first == crack.store.stem(long)
    assert crack.store._STREAM.match(f"attempts/{first}")


def test_packet_of_a_candidate_that_did_not_link_has_no_diff(lane, monkeypatch):
    snapshot, _ = lane
    _candidate(snapshot)
    unlinked = replace(_proof(0.0), built_sha256="")
    _measurement(snapshot, monkeypatch, proofs=(unlinked,))
    crack.store.cached.side_effect = AssertionError("an unlinked candidate has no built bytes to read")
    import json
    assert json.loads(crack.packet(snapshot, MEMBER).read_text())["diff"] == ""
    crack.symptoms.diff.assert_not_called()


def test_run_refuses_a_fragment_before_any_work(lane, monkeypatch):
    snapshot, _ = lane
    monkeypatch.setattr(crack.layout, "capture", Mock(return_value=snapshot))
    monkeypatch.setattr(crack.draft, "split_slot", Mock(return_value="func_80000404"))
    with pytest.raises(Refusal) as caught:
        crack.run(snapshot.config, {"item": MEMBER, "seconds": 2})
    finding, = caught.value.findings
    assert finding.key == "member.split-delay-slot" and "func_80000404" in finding.reason
    assert finding.unit == MEMBER


def test_packet_reads_assembly_only_from_a_version_holding_the_member(lane, monkeypatch):
    snapshot, _ = lane
    member = replace(snapshot.layout.members[MEMBER], placements=snapshot.layout.members[MEMBER].placements[1:])
    snapshot = replace(snapshot, layout=replace(snapshot.layout, members={MEMBER: member}))
    _candidate(snapshot)
    _measurement(snapshot, monkeypatch, proofs=(_proof(0.4, "b"),))
    asm = crack.versions.asm_path(snapshot.config, "b", MEMBER)
    asm.parent.mkdir(parents=True)
    asm.write_text("jal callee\n")
    crack.versions.asm_path(snapshot.config, "a", MEMBER).write_text("jal wrong\n")
    path = crack.packet(snapshot, MEMBER)
    assert list(json.loads(path.read_text())["target"]) == ["b"]
    assert "callee" in crack.types.declarations.call_args.args[1]
