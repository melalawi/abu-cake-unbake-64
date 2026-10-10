"""W24 report behavior, using immutable records and mocked module boundaries."""

import hashlib
import json
import struct
from contextlib import nullcontext
from dataclasses import replace
from unittest.mock import Mock

import fixture
import pytest

from unbake import config as configuration
from unbake import report
from unbake.contracts import (
    Config,
    Group,
    Host,
    LayoutMap,
    Member,
    Placement,
    Project,
    Refusal,
    Snapshot,
    UnitSpec,
    Version,
    VersionFiles,
)


def _member(name, *, size=16, address=0x80000400, kind="function", group="g",
            versions=("a", "b"), section=".text", state="asm"):
    return Member(name, kind, state, group, tuple(
        Placement(v, section, 0x1000, 0x1000 + size, address) for v in versions
    ))


def _unit(name, kind="c"):
    return UnitSpec(f"src/{name}.{kind}", kind, "g", (name,), "test", {})


@pytest.fixture
def snapshot_factory(tmp_path, monkeypatch):
    monkeypatch.setattr(report.effort, "stage", lambda name: nullcontext())
    monkeypatch.setattr(report.layout, "unit_of", lambda s, m: next(
        (u for u in s.layout.units.values() if m in u.members), None
    ))
    boundary = {k: {"applied": 0, "proposed": 0, "withheld": 0} for k in ("prelude", "split", "merge")}
    monkeypatch.setattr(report.layout, "boundary_plan", Mock(return_value=(None, boundary)))
    monkeypatch.setattr(report.draft, "split_slot", lambda s, m: None)
    monkeypatch.setattr(report.crack, "state", lambda s, m: "fuzzy" if m in s.layout.fuzzy else "open")
    monkeypatch.setattr(report.crack, "history", Mock(return_value=[]))

    def make(members=(), *, units=(), fuzzy=None, groups=None, versions=("a", "b"), overlays=None):
        vf = {v: VersionFiles(f"{v}.z64", "0" * 40, f"{v}.yaml", f"{v}.txt", {}) for v in versions}
        project = Project(tmp_path, "fixture", "fixture", "Fixture", versions, versions[0],
                          "test", {}, vf, {}, {}, 200, {}, "project")
        host = Host(2, 2, 1024, 1024, 1024, tmp_path / "tools", {},
                    None, 2, 2, 0.8, 2, ("test", "test@example.invalid"), {}, "host")
        config = Config(project, host, "config")
        groups = groups if groups is not None else [Group(
            "g", "main", tuple(m.name for m in members), "authored", (), "unknown", False
        )]
        lm = LayoutMap(200, {g.name: g for g in groups}, {m.name: m for m in members},
                       {u.path: u for u in units}, "layout", (), fuzzy or {})
        vs = {v: Version(v, tmp_path / f"{v}.z64", "a" * 64, vf[v].split,
                         vf[v].symbols, {}, ()) for v in versions}
        marks = configuration.load_resource("repo.toml")["readme"]
        contents = {"README.md": f"{marks['begin']}\nold\n{marks['end']}\n".encode()}
        contents.update(overlays or {})
        return Snapshot(config, "b" * 40, lm, vs, contents, "snapshot")

    return make


def test_credit_is_layout_state(snapshot_factory, monkeypatch):
    members = [_member("credited"), _member("assembly"), _member("ledger_only", state="c")]
    snapshot = snapshot_factory(members, units=[_unit("credited"), _unit("assembly", "asm")],
                                overlays={".unbake/ledger.jsonl": b'{"member":"ledger_only","exact":true}\n'})
    real_read = Snapshot.read
    reads = []

    def read(s, path):
        reads.append(path)
        assert "ledger" not in path
        return real_read(s, path)

    monkeypatch.setattr(Snapshot, "read", read)
    result = report.current(snapshot)
    for version in ("a", "b"):
        assert result["versions"][version] == {
            "code_total": 48, "code_matched": 16, "data_total": 0, "data_matched": 0,
            "functions_total": 3, "functions_matched": 1, "code_fuzzy": 0, "data_fuzzy": 0, "fuzzy_bytes": 0,
            "code_percent": 33.33, "data_percent": 0.0, "fuzzy_percent": 33.33,
        }
        assert result["kinds"]["c"][version]["code_matched"] == 16
        assert result["kinds"]["asm"][version]["code_matched"] == 0
    assert reads == [".unbake/check.json"]


def test_opaque_rom_bytes_stay_out_of_the_denominator(snapshot_factory):
    opaque = _member("blob", size=1000, kind="data", section=".data", state="bin")
    snapshot = snapshot_factory([_member("credited"), opaque], units=[_unit("credited")])
    result = report.current(snapshot)
    for version in ("a", "b"):
        assert result["versions"][version]["data_total"] == 0
        assert result["versions"][version]["fuzzy_percent"] == 100.0
        assert "bin" not in result["kinds"]


def test_unknown_group_counts_as_unknown_subsystem(snapshot_factory):
    snapshot = snapshot_factory([_member("ungrouped", group="")], groups=[])
    result = report.current(snapshot)
    assert result["subsystems"]["unknown"] == result["versions"]


@pytest.mark.parametrize("score, code_fuzzy, data_fuzzy, fuzzy_percent", [(0.0, 0, 0, 25.0), (0.25, 5, 2, 33.75),
                                                                     (0.5, 10, 4, 42.5), (0.95, 20, 9, 61.25)])
def test_fuzzy_bytes_and_percent(snapshot_factory, score, code_fuzzy, data_fuzzy, fuzzy_percent):
    matched = _member("matched", size=20)
    fuzzy = replace(_member("fuzzy", size=21), placements=(
        Placement("a", ".text", 0x1000, 0x1015, 0x80000400),
        Placement("a", ".rodata", 0x2000, 0x2009, 0x80000500),
        Placement("a", ".bss", 0, 0, 0x80000600, 999),
    ))
    data = _member("data", size=30, kind="data", section=".data", state="data", versions=("a",))
    snapshot = snapshot_factory([matched, fuzzy, data], units=[_unit("matched")],
                                fuzzy={"fuzzy": {"path": "src/fuzzy/fuzzy.c", "scores": {"a": score}}})
    tally = report.current(snapshot)["versions"]["a"]
    assert tally == {"code_total": 41, "code_matched": 20, "data_total": 39, "data_matched": 0,
                     "functions_total": 2, "functions_matched": 1, "code_fuzzy": code_fuzzy,
                     "data_fuzzy": data_fuzzy, "fuzzy_bytes": code_fuzzy + data_fuzzy,
                     "code_percent": 48.78, "data_percent": 0.0, "fuzzy_percent": fuzzy_percent}


@pytest.mark.parametrize("document, expected", [
    (None, {"stale": True, "action": "unbake check"}),
    (b"not json", {"stale": True, "action": "unbake check"}),
    (b'{"commit":"old","counts":{}}', {"stale": True, "action": "unbake check"}),
    (json.dumps({"commit": "b" * 40, "counts": {"debt": 3}}).encode(),
     {"commit": "b" * 40, "counts": {"debt": 3}}),
])
def test_debt_and_boundary(snapshot_factory, monkeypatch, document, expected):
    snapshot = snapshot_factory(overlays={".unbake/check.json": document})
    counts = {"prelude": {"applied": 2, "proposed": 3, "withheld": 1},
              "split": {"applied": 3, "proposed": 3, "withheld": 0},
              "merge": {"applied": 1, "proposed": 3, "withheld": 2}}
    monkeypatch.setattr(report.layout, "boundary_plan", Mock(return_value=(None, counts)))
    result = report.current(snapshot)
    assert result["debt"] == expected
    assert result["boundary"] == counts
    assert result["pending_boundary"] == 6
    assert result["commit"] == snapshot.commit
    assert result["versions"]["a"]["code_percent"] == 0.0


def test_items_order_rank_state_size(snapshot_factory, monkeypatch):
    specs = [("creative", "creative", 1, 10), ("fuzzy", "fuzzy", 1, 10),
             ("tool", "tool", 1, 10), ("large", "open", 32, 1),
             ("late", "open", 16, 30), ("z_tie", "open", 16, 10),
             ("a_tie", "open", 16, 10)]
    members = [_member(n, size=size, address=addr, group="math") for n, _, size, addr in specs]
    members += [_member("sdk_creative", size=999, group="sdk"), _member("matched", group="math")]
    groups = [Group(s, "main", tuple(m.name for m in members if m.group == s),
                    "authored", (), s, False) for s in ("sdk", "math")]
    snapshot = snapshot_factory(members, groups=groups, units=[_unit("matched")],
                                fuzzy={"fuzzy": {"path": "src/fuzzy/fuzzy.c", "scores": {"a": .7, "b": .4}}})
    states = {n: state for n, state, *_ in specs} | {"sdk_creative": "creative"}
    monkeypatch.setattr(report.crack, "state", lambda s, m: states[m])
    history = Mock(return_value=[fixture.attempt(score=.2), fixture.attempt(score=.8)])
    monkeypatch.setattr(report.crack, "history", history)
    rows = report.items(snapshot, {})
    assert [r["member"] for r in rows] == ["sdk_creative", "large", "a_tie", "z_tie", "late",
                                           "tool", "fuzzy", "creative"]  # biggest first, sub-16-byte fragments last
    seconds = configuration.load_resource("flow.toml")["work"]["permuter_seconds"]
    for row in rows:
        name = row["member"]
        assert row["best"] == (.4 if name == "fuzzy" else 0.0 if row["state"] == "open" else .8)
        command = (f"unbake crack {name} --seconds {seconds}" if row["state"] in ("open", "tool")
                   else f"unbake compare .unbake/work/{name}.c --function {name}")
        assert row["command"] == command
        if row["state"] == "creative":
            assert row["packet"] == f".unbake/packets/{name}.json"
    assert all(c.args[0] is snapshot.config for c in history.call_args_list)
    assert not {"fuzzy", "large", "late", "z_tie", "a_tie"} & {c.args[1] for c in history.call_args_list}  # no file, no read
    assert [r["member"] for r in report.items(snapshot, {"subsystem": "sdk"})] == ["sdk_creative"]


def test_items_candidates_and_holder_fallback(snapshot_factory):
    members = [_member("only_b", versions=("b",), address=88),
               _member("data", kind="data", section=".rodata", state="data", size=12),
               _member("bss", kind="data", section=".bss", state="data"),
               _member("no_text", section=".rodata")]
    rows = report.items(snapshot_factory(members), {})
    assert [r["member"] for r in rows] == ["only_b", "data"]
    assert rows[0]["address"] == 88
    assert rows[0]["best"] == 0.0


def test_items_send_a_withheld_member_to_its_existing_source(snapshot_factory):
    unit = replace(_unit("partial"), withheld=("b",))
    rows = report.items(snapshot_factory([_member("partial")], units=[unit]), {})
    assert [(r["member"], r["command"]) for r in rows] == [
        ("partial", f"unbake compare {unit.path} --function partial")]


@pytest.mark.parametrize("modulus", [1, 2, 3, 7, 31])
def test_items_shards_disjoint_and_complete(snapshot_factory, modulus):
    snapshot = snapshot_factory([_member(f"m{i}") for i in range(40)])
    shards = [{r["member"] for r in report.items(snapshot, {"shard": f"{i}/{modulus}"})}
              for i in range(modulus)]
    all_names = set(snapshot.layout.members)
    assert set.union(*shards) == all_names
    assert sum(map(len, shards)) == len(all_names)
    for i, names in enumerate(shards):
        assert names == {m for m in all_names if int(hashlib.sha256(m.encode()).hexdigest()[:8], 16) % modulus == i}


@pytest.mark.parametrize("shard", ["", "1", "1/0", "0/0", "2/2", "3/2", "-1/2", "0/-2",
                                  "1.0/2", "x/2", "0/2/3", " 0/2", "0/2\n", 2, True])
def test_items_bad_shard_refuses(snapshot_factory, shard):
    with pytest.raises(Refusal) as error:
        report.items(snapshot_factory(), {"shard": shard})
    assert error.value.findings[0].key == "report.request"


@pytest.mark.parametrize("count", [None, 1, 2, 10, 0, -1, 1.5, "2", True])
def test_items_count(snapshot_factory, monkeypatch, count):
    snapshot = snapshot_factory([_member(f"m{i}") for i in range(3)])
    history = report.crack.history
    if count is not None and (type(count) is not int or count < 1):
        with pytest.raises(Refusal) as error:
            report.items(snapshot, {"count": count})
        assert error.value.findings[0].key == "report.request"
        return
    rows = report.items(snapshot, {"count": count})
    assert len(rows) == (3 if count is None else min(count, 3))
    assert [r["member"] for r in rows] == [f"m{i}" for i in range(len(rows))]
    history.assert_not_called()  # an open member has no attempts file to read


@pytest.mark.parametrize("subsystem", ["not-a-subsystem", "", 1, True])
def test_items_bad_subsystem_refuses(snapshot_factory, subsystem):
    with pytest.raises(Refusal) as error:
        report.items(snapshot_factory(), {"subsystem": subsystem})
    assert error.value.findings[0].key == "report.request"


def test_objdiff_validates_and_is_deterministic(snapshot_factory, monkeypatch):
    members = [_member("open", address=500, size=30), _member("fuzzy", address=300, size=20),
               _member("matched", address=100, size=10, group="sdk"),
               _member("data", address=400, kind="data", section=".data", size=12),
               _member("other_version", versions=("b",))]
    groups = [Group("g", "main", ("open", "fuzzy", "data"), "authored", (), "unknown", False),
              Group("sdk", "main", ("matched",), "authored", (), "sdk", False)]
    snapshot = snapshot_factory(members, groups=groups, units=[_unit("matched"), _unit("data", "data")],
                                fuzzy={"fuzzy": {"path": "src/fuzzy/fuzzy.c", "scores": {"a": .333333333, "b": .5}}})
    validation = Mock(wraps=configuration.validate)
    monkeypatch.setattr(configuration, "validate", validation)
    current = report.current(snapshot)
    first = report.objdiff(snapshot, current, "a")
    second = report.objdiff(snapshot, current, "a")
    def encode(value):
        return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    assert encode(first) == encode(second)
    assert first["version"] == 2  # an unversioned report is migrated by objdiff, doubling every complete_* sum
    assert first["measures"]["complete_code"] == str(
        sum(int(u["measures"].get("complete_code", 0)) for u in first["units"]))
    assert validation.call_count == 2
    validation.assert_called_with("objdiff", second, "versions/a/report.json")
    assert [u["name"] for u in first["units"]] == ["matched", "fuzzy", "data", "open"]
    by_name = {u["name"]: u for u in first["units"]}
    assert by_name["matched"]["metadata"] == {
        "complete": True, "progress_categories": ["sdk"], "source_path": "src/matched.c"}
    assert by_name["matched"]["measures"] == {
        "total_code": "10", "total_units": 1, "total_functions": 1, "fuzzy_match_percent": 100.0,
        "matched_code": "10", "complete_code": "10", "matched_code_percent": 100.0,
        "complete_code_percent": 100.0, "complete_units": 1, "matched_functions": 1,
        "matched_functions_percent": 100.0}
    assert by_name["fuzzy"]["metadata"]["source_path"] == "src/fuzzy/fuzzy.c"
    assert by_name["fuzzy"]["functions"] == [{"name": "fuzzy", "size": "20", "address": "300",
                                                "fuzzy_match_percent": 33.333332, "metadata": {}}]
    assert by_name["open"]["functions"] == [{"name": "open", "size": "30", "address": "500", "metadata": {}}]
    assert by_name["data"]["functions"] == []
    assert by_name["data"]["measures"]["matched_data"] == "12"
    assert by_name["data"]["sections"] == [{"name": ".data", "size": "12",
                                              "fuzzy_match_percent": 100.0, "metadata": {}}]
    assert first["measures"]["total_code"] == "60"
    assert first["measures"]["matched_code_percent"] == 16.666666
    # objdiff weights the f32 per-unit value (33.333332), not the unrounded input
    assert first["measures"]["fuzzy_match_percent"] == 27.777777
    assert [c["id"] for c in first["categories"]] == ["sdk", "unknown"]
    assert first["categories"][0]["name"] == configuration.load_resource("subsystems.toml")["subsystem"][0]["label"]
    assert first["categories"][0]["measures"]["matched_code_percent"] == 100.0


@pytest.mark.parametrize("value, expected", [(0, 0.0), (100, 100.0), (1 / 3, .33333334),
                                          (33.333333333, 33.333332), (72.64, 72.64),
                                          (1.23456789, 1.2345679), (1e-30, 1e-30)])
def test_f32_shortest(value, expected):
    result = report._f32(value)
    assert result == expected
    assert struct.pack(">f", result) == struct.pack(">f", value)
    digits = len(format(result, ".8e").split("e")[0].rstrip("0").replace(".", "").lstrip("-"))
    for precision in range(1, digits):
        rounded = float(f"{result:.{precision}g}")
        assert struct.pack(">f", rounded) != struct.pack(">f", value)


def _readme_case(snapshot_factory):
    snapshot = snapshot_factory(versions=("us", "eu"))
    files = dict(snapshot.config.project.version_files)
    files["us"] = replace(files["us"], meta={"cartridge_id": "NUS-NRWE-0", "region": "North America",
                                           "description": "First NTSC release (black cartridge)."})
    snapshot = replace(snapshot, config=replace(snapshot.config, project=replace(
        snapshot.config.project, version_files=files)))
    values = {"us": {"code_total": 1_088_548, "code_matched": 816_136, "data_total": 0,
                      "data_matched": 0, "code_fuzzy": 18_070, "data_fuzzy": 0,
                      "functions_total": 4196, "functions_matched": 3769},
              "eu": {"code_total": 4_467_256, "code_matched": 3_219_816, "data_total": 0,
                      "data_matched": 0, "code_fuzzy": 165_330, "data_fuzzy": 0,
                      "functions_total": 0, "functions_matched": 0}}
    return snapshot, {"versions": values}


def test_readme_block_exact(snapshot_factory):
    snapshot, current = _readme_case(snapshot_factory)
    begin = "<!-- progress -->"
    end = "<!-- /progress -->"
    expected = (
        f"{begin}\n"
        "<pre><code>all  [██████████████▒░░░░░]  72.64% (~75.94%)  4,035,952 of 5,555,804 bytes</code>"
        "<br><code>us   [██████████████▒░░░░░]  74.97% (~76.63%)  816,136 of 1,088,548 bytes</code>"
        "<br><code>eu   [██████████████▒░░░░░]  72.08% (~75.78%)  3,219,816 of 4,467,256 bytes</code></pre>\n\n"
        "| us (NUS-NRWE-0, North America). First NTSC release (black cartridge). SHA256 `" + "a" * 64 + "` |\n"
        "|---|\n"
        "| <pre><code>code      [██████████████▒░░░░░]  74.97% (~76.63%)  816,136 of 1,088,548</code>"
        "<br><code>data      [░░░░░░░░░░░░░░░░░░░░]   0.00% (~0.00%)  0 of 0</code>"
        "<br><code>functions [█████████████████░░░]  89.82%  3,769 of 4,196</code></pre> |\n\n"
        "| eu. SHA256 `" + "a" * 64 + "` |\n|---|\n"
        "| <pre><code>code      [██████████████▒░░░░░]  72.08% (~75.78%)  3,219,816 of 4,467,256</code>"
        "<br><code>data      [░░░░░░░░░░░░░░░░░░░░]   0.00% (~0.00%)  0 of 0</code>"
        "<br><code>functions [░░░░░░░░░░░░░░░░░░░░]   0.00%  0 of 0</code></pre> |\n"
        f"{end}"
    )
    assert report.readme(f"{begin}\nold\n{end}", snapshot, current) == expected


def test_readme_outside_markers_untouched(snapshot_factory):
    snapshot, current = _readme_case(snapshot_factory)
    settings = configuration.load_resource("repo.toml")["readme"]
    prefix, suffix = "# Title é\r\n  before\t", "\r\nAfter Ω\n\x00"
    text = prefix + settings["begin"] + "old content\r\n" + settings["end"] + suffix
    rendered = report.readme(text, snapshot, current)
    assert rendered.startswith(prefix + settings["begin"] + "\n")
    assert rendered.endswith("\n" + settings["end"] + suffix)
    assert report.readme(rendered, snapshot, current) == rendered


@pytest.mark.parametrize("text", ["", "<!-- progress -->", "<!-- /progress -->",
                                 "<!-- /progress --><!-- progress -->",
                                 "<!-- progress --><!-- progress --><!-- /progress -->",
                                 "<!-- progress --><!-- /progress --><!-- /progress -->"])
def test_readme_missing_markers_refuses(snapshot_factory, text):
    snapshot, current = _readme_case(snapshot_factory)
    with pytest.raises(Refusal) as error:
        report.readme(text, snapshot, current)
    finding, = error.value.findings
    assert (finding.key, finding.reason, finding.path) == (
        "repo.readme_markers", "README.md needs one begin and one end progress marker", "README.md")


def test_files_canonical_and_complete(snapshot_factory, monkeypatch):
    monkeypatch.setattr(report.pool, "map", lambda cfg, name, fn, items, key=None: [fn(i) for i in items])
    snapshot = snapshot_factory([_member("matched")], units=[_unit("matched")])
    first = report.files(snapshot)
    assert first == report.files(snapshot)
    assert set(first) == {"README.md", "versions/a/report.json", "versions/b/report.json"}
    current = report.current(snapshot)
    for v in snapshot.config.project.versions:
        content = first[f"versions/{v}/report.json"]
        assert content == json.dumps(report.objdiff(snapshot, current, v), sort_keys=True,
                                     separators=(",", ":")).encode() + b"\n"
    assert first["README.md"] == report.readme(snapshot.read("README.md").decode(), snapshot, current).encode()


@pytest.mark.parametrize("next_mode, empty", [(False, False), (False, True), (True, False), (True, True)])
def test_run_results_validate(snapshot_factory, monkeypatch, next_mode, empty):
    snapshot = snapshot_factory([] if empty else [_member("candidate")])
    capture = Mock(return_value=snapshot)
    inbox = Mock(return_value=[fixture.submission(), fixture.submission(member="another")])
    monkeypatch.setattr(report.layout, "capture", capture)
    monkeypatch.setattr(report.land, "inbox", inbox)
    result = report.run(snapshot.config, {"next": next_mode})
    configuration.validate("result.next" if next_mode else "result.report", result, "result")
    capture.assert_called_once_with(snapshot.config)
    if next_mode:
        assert result == {"items": report.items(snapshot, {"next": True})}
        inbox.assert_not_called()
    else:
        assert result["inbox"] == 2
        assert result["next"] == ("" if empty else report.items(snapshot, {"count": 1})[0]["command"])
        inbox.assert_called_once_with(snapshot.config)


def test_items_leave_out_fragments_whose_delay_slot_is_in_the_next_member(snapshot_factory, monkeypatch):
    monkeypatch.setattr(report.draft, "split_slot", lambda snap, name: "next" if name == "cut" else None)
    rows = report.items(snapshot_factory([_member("cut"), _member("whole", address=96)]), {})
    assert [r["member"] for r in rows] == ["whole"]
