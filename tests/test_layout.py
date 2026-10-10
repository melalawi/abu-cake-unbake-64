"""Layout regressions with mocked process calls and memory-only ROM material."""
from __future__ import annotations

import hashlib
import tomllib
from contextlib import nullcontext
from dataclasses import replace
from unittest.mock import Mock

import fixture
import pytest
import yaml

from unbake import layout
from unbake.contracts import (
    Claim,
    Config,
    Group,
    Host,
    LayoutMap,
    Placement,
    Project,
    Refusal,
    Snapshot,
    UnitSpec,
    Version,
    digest,
)

BASE = 0x80000400
RETURN = (0x03E00008, 0)


@pytest.fixture
def scene(tmp_path, monkeypatch):
    """Create contract records and YAML files, without fixture.project's git or ROMs."""
    monkeypatch.setattr(layout.effort, "stage", lambda name: nullcontext())
    monkeypatch.setattr(layout.process, "git", Mock(return_value=fixture.native_result(stdout=b"head\n")))
    host = Host(2, 2, 1 << 30, 1 << 30, 1 << 30,
                tmp_path / "toolchains", {}, None, 2, 2, 0.8, 2, ("test", "test@example.invalid"), {}, "host")
    project = Project(tmp_path, "fixture", "fixture", "Fixture", ("a", "b"), "a", "gcc-test",
                      {}, {}, {}, {}, 200, {}, "project")
    config = Config(project, host, "config")
    blobs = {}

    def rows(version, reader, cache=None):
        subs = yaml.safe_load(reader(version.split))["segments"][0]["subsegments"]
        return [(row[2], row[1], Placement(version.id, ".text" if row[1] in ("asm", "c") else ".data",
                row[0], subs[i + 1][0], BASE + row[0] - 0x1000))
                for i, row in enumerate(subs[:-1])]

    monkeypatch.setattr(layout.version_data, "rows", rows)
    monkeypatch.setattr(layout.version_data, "rom_bytes",
                        lambda version, start, end: blobs[version.id][start - 0x1000:end - 0x1000])

    def make(entries=(("func_80000400", "asm", RETURN),), *, other=None, units=(), authored=(), fuzzy=None,
             grouped=True):
        versions, named = {}, {}
        for vid, items in (("a", entries), ("b", entries if other is None else other)):
            subs, blob = [], b""
            for name, state, words in items:
                at = 0x1000 + len(blob)
                subs.append(f"      - [0x{at:X}, {state}, {name}]\n")
                named.setdefault(name, {"kind": "function"})[vid] = BASE + len(blob)
                blob += b"".join(word.to_bytes(4, "big") for word in words)
            subs.append(f"      - [0x{0x1000 + len(blob):X}]\n")
            split = f"versions/{vid}/Game.yaml"
            sym = f"versions/{vid}/symbol_addrs.txt"
            (tmp_path / split).parent.mkdir(parents=True, exist_ok=True)
            (tmp_path / split).write_text("segments:\n  - name: main\n    subsegments:\n" + "".join(subs))
            (tmp_path / sym).write_bytes(layout.symbols.render(named, vid))
            blobs[vid] = blob
            versions[vid] = Version(vid, tmp_path / f"unused-{vid}", hashlib.sha256(blob).hexdigest(), split, sym,
                                    {}, (("main", 0x1000, 0x1000 + len(blob), BASE),))
        (tmp_path / "symbols.toml").write_bytes(layout.symbols.dump(named))
        groups = {"grp": Group("grp", "main", tuple(n for n, _, _ in entries), "authored", (), False)}
        initial = LayoutMap(200, groups if grouped else {}, {}, {u.path: u for u in units}, "", authored, fuzzy or {})
        text = layout.dump_map(initial)
        (tmp_path / "layout.toml").write_bytes(text)
        monkeypatch.setattr(layout.version_data, "read", Mock(return_value=versions))
        loaded = layout.load_map(config, versions, text, lambda p: (tmp_path / p).read_bytes())
        return Snapshot(config, "head", loaded, versions, {}, "snapshot")

    return make


def _unit(members=("func_80000400",), path="src/grp.c", kind="c"):
    return UnitSpec(path, kind, "grp", members, "gcc-test", {"add": ["-O1"], "omit": ["-O2"]})


def _load(snapshot, text):
    return layout.load_map(snapshot.config, snapshot.versions, text, snapshot.read)


def test_load_dump_revision_3(scene):
    authored = ({"version": "a", "rom": 0x1000, "kind": "function", "name": "func_80000400"},)
    snapshot = scene(units=(_unit(),), authored=authored)
    result = _load(snapshot, layout.dump_map(snapshot.layout))
    assert result == snapshot.layout
    assert result.authored == authored
    assert result.units["src/grp.c"] == _unit()
    assert result.members["func_80000400"].holders() == ("a", "b")
    assert result.members["func_80000400"].group == "grp"


def test_ungrouped_member_and_unit_of(scene):
    snapshot = scene(grouped=False)
    assert snapshot.layout.members["func_80000400"].group == ""
    assert layout.unit_of(snapshot, "func_80000400") is None
    assert layout.unit_of(snapshot, "missing") is None
    with_unit = replace(snapshot, layout=replace(snapshot.layout, units={"src/grp.c": _unit()}))
    assert layout.unit_of(with_unit, "func_80000400") == _unit()


def test_fuzzy_roundtrip(scene):
    entries = (("zeta", "asm", RETURN), ("alpha", "asm", RETURN))
    fuzzy = {name: {"path": f"src/fuzzy/{name}.c", "scores": {"b": 0.7, "a": 0.4}} for name, _, _ in entries}
    snapshot = scene(entries, fuzzy=fuzzy)
    dumped = layout.dump_map(snapshot.layout)
    assert _load(snapshot, dumped) == snapshot.layout
    assert snapshot.layout.fuzzy == fuzzy
    rows = tomllib.loads(dumped.decode())["fuzzy"]
    assert [row["member"] for row in rows] == ["alpha", "zeta"]
    assert all(list(row["scores"]) == ["a", "b"] for row in rows)
    assert 'scores = {a = 0.4, b = 0.7}' in dumped.decode()


def test_fuzzy_unknown_member_refuses_layout_map(scene):
    snapshot = scene()
    text = layout.dump_map(replace(snapshot.layout, fuzzy={
        "missing": {"path": "src/fuzzy/missing.c", "scores": {"a": 0.1}},
    }))
    with pytest.raises(Refusal) as caught:
        _load(snapshot, text)
    assert caught.value.findings[0].key == "layout.map"
    assert caught.value.findings[0].missing == ("missing",)


def test_dump_schema_3(scene):
    text = layout.dump_map(scene().layout)
    doc = tomllib.loads(text.decode())
    assert doc["schema"] == 3
    assert doc["unit"] == []
    assert "authored" not in doc and "fuzzy" not in doc


@pytest.mark.parametrize("text", [b"[bad", b"\xff", b"schema = 2\ncap = 200\ngroup = []\nunit = []\n"])
def test_invalid_layout_refuses(scene, text):
    snapshot = scene()
    with pytest.raises(Refusal):
        _load(snapshot, text)


def test_unit_unknown_member_refuses(scene):
    snapshot = scene()
    text = layout.dump_map(replace(snapshot.layout, units={"src/grp.c": _unit(("missing",))}))
    with pytest.raises(Refusal) as caught:
        _load(snapshot, text)
    assert caught.value.findings[0].key == "layout.map"
    assert caught.value.findings[0].missing == ("missing",)


def test_capture_retries_when_head_moves(scene, monkeypatch):
    snapshot = scene()
    git = Mock(side_effect=[fixture.native_result(stdout=f"{head}\n".encode()) for head in ("a", "b", "b", "b")])
    monkeypatch.setattr(layout.process, "git", git)
    captured = layout.capture(snapshot.config)
    assert captured.commit == "b"
    assert git.call_count == 4
    assert layout.version_data.read.call_count == 1  # the retry reads the same inputs, so the capture is cached
    assert all(call.args == (snapshot.config, "rev-parse", "HEAD") for call in git.call_args_list)


def test_capture_digest_ignores_the_commit(scene, monkeypatch):
    snapshot = scene()
    monkeypatch.setattr(layout.process, "git", Mock(side_effect=[fixture.native_result(stdout=f"{h}\n".encode())
                                                                  for h in ("a", "a", "b", "b")]))
    assert layout.capture(snapshot.config).digest == layout.capture(snapshot.config).digest


def test_capture_busy_after_three(scene, monkeypatch):
    snapshot = scene()
    git = Mock(side_effect=[fixture.native_result(stdout=str(i).encode()) for i in range(6)])
    monkeypatch.setattr(layout.process, "git", git)
    with pytest.raises(Refusal) as caught:
        layout.capture(snapshot.config)
    finding = caught.value.findings[0]
    assert (finding.key, finding.reason, finding.action) == (
        "layout.busy", "HEAD moved while reading the project three times", "retry the command",
    )
    assert git.call_count == 6
    assert layout.version_data.read.call_count == 1


@pytest.mark.parametrize("version", ["a", "b"])
@pytest.mark.parametrize("grouped", [True, False])
def test_asm_unit_paths_and_kind(scene, monkeypatch, version, grouped):
    snapshot = scene(grouped=grouped)
    path = snapshot.config.project.root / f"versions/{version}/asm/func_80000400.s"
    asm_path = Mock(return_value=path)
    monkeypatch.setattr(layout.version_data, "asm_path", asm_path)
    unit = layout.asm_unit(snapshot, "func_80000400", version)
    assert unit == UnitSpec(path.relative_to(snapshot.config.project.root).as_posix(), "asm",
                           "grp" if grouped else "", ("func_80000400",), "gcc-test", {"add": [], "omit": []})
    asm_path.assert_called_once_with(snapshot.config, version, "func_80000400")


def test_ungrouped_member_is_refused_by_name_when_a_unit_is_built(scene):
    snapshot = scene(grouped=False)
    with pytest.raises(Refusal) as caught:
        layout.unit_options(snapshot, "func_80000400", b"source")
    finding = caught.value.findings[0]
    assert (finding.key, finding.unit, finding.action) == ("layout.member", "func_80000400", "run unbake setup")


@pytest.mark.parametrize("member,version", [("missing", "a"), ("func_80000400", "missing"), ("datum", "a")])
def test_asm_unit_no_text_refuses(scene, member, version):
    snapshot = scene((("func_80000400", "asm", RETURN), ("datum", "data", (0,))))
    with pytest.raises(Refusal) as caught:
        layout.asm_unit(snapshot, member, version)
    assert caught.value.findings[0].key == "layout.member"
    assert layout._edit(b"  - [0x1000, .data, old]\n", "old", rename="new") == b"  - [0x1000, .data, new]\n"


def test_overlay_content_deletion_and_digest(scene):
    snapshot = scene()
    writes = {"src/new.c": b"source", "gone": None}
    updated = layout.overlay(snapshot, writes)
    assert updated.read("src/new.c") == b"source"
    with pytest.raises(FileNotFoundError):
        updated.read("gone")
    pins = [("gone", None), ("src/new.c", hashlib.sha256(b"source").hexdigest())]
    assert updated.digest == digest((snapshot.digest, pins))
    assert updated.layout is snapshot.layout
    assert snapshot.overlays == {}
    again = layout.overlay(updated, {"src/new.c": b"replacement"})
    assert again.overlays == {"src/new.c": b"replacement", "gone": None}


@pytest.mark.parametrize("watched", ["layout.toml", "versions/a/Game.yaml", "symbols.toml"])
def test_overlay_reloads_watched_files(scene, watched):
    snapshot = scene()
    old_calls = layout.version_data.read.call_count
    updated = layout.overlay(snapshot, {watched: snapshot.read(watched)})
    assert updated.layout == snapshot.layout
    assert layout.version_data.read.call_count == old_calls + 1


def test_unit_options_standalone_and_overlay(scene):
    snapshot = scene()
    options = layout.unit_options(snapshot, "func_80000400", b"void f(void) {}\n")
    assert len(options) == 1
    unit, writes = options[0]
    assert unit.path == "src/grp.c" and unit.members == ("func_80000400",)
    proposed = layout.overlay(snapshot, writes)
    assert layout.unit_of(proposed, "func_80000400") == unit
    assert proposed.read(unit.path) == b"void f(void) {}\n"
    assert snapshot.layout.units == {}
    for version in snapshot.versions.values():
        assert b", c, func_80000400]" in proposed.read(version.split)
        assert b", asm, func_80000400]" in snapshot.read(version.split)


@pytest.mark.parametrize("adjacent", [True, False])
def test_unit_options_append_requires_all_holders(scene, adjacent):
    entries = (("first", "c", RETURN), ("target", "asm", RETURN), ("gap", "asm", RETURN))
    other = entries if adjacent else (entries[0], entries[2], entries[1])
    unit = _unit(("first",))
    snapshot = scene(entries, other=other, units=(unit,))
    (snapshot.config.project.root / unit.path).parent.mkdir(exist_ok=True)
    (snapshot.config.project.root / unit.path).write_bytes(b"first source")
    options = layout.unit_options(snapshot, "target", b"target source")
    assert len(options) == (2 if adjacent else 1)
    assert options[-1][0].path == "src/target.c"
    if adjacent:
        extended, writes = options[0]
        assert extended.members == ("first", "target") and extended.options == unit.options
        assert writes[unit.path] == b"first source\ntarget source"


@pytest.mark.parametrize("state,member", [("c", "func_80000400"), ("asm", "missing")])
def test_unit_options_refuses(scene, state, member):
    snapshot = scene((("func_80000400", state, RETURN),))
    with pytest.raises(Refusal) as caught:
        layout.unit_options(snapshot, member, b"source")
    assert caught.value.findings[0].key == "layout.member"


@pytest.mark.parametrize("rule", ["prelude", "split", "merge"])
@pytest.mark.parametrize("guard", ["none", "authored", "compiled", "disagree"])
def test_boundary_rules_and_guards(scene, monkeypatch, rule, guard):
    first = ("func_80000400", "asm", RETURN if rule != "merge" else (0x24020001, 0))
    words = {"prelude": (0, 0, *RETURN), "split": (*RETURN, *RETURN), "merge": RETURN}[rule]
    target = ("func_80000408", "c" if guard == "compiled" else "asm", words)
    entries = (first, target)
    authored = ()
    if guard == "authored":
        authored = ({"version": "a", "rom": 0x1008, "kind": "function", "name": target[0]},)
    snapshot = scene(entries, authored=authored)
    refs = frozenset({BASE + 16}) if rule != "merge" else frozenset()

    def decoded(snap, version, rows):
        references = frozenset({BASE + 8}) if guard == "disagree" and version.id == "b" else refs
        return {first[0]: first[2], target[0]: words}, references

    monkeypatch.setattr(layout, "_decoded", decoded)
    plan, counts = layout.boundary_plan(snapshot)
    assert plan.operation == "layout" and plan.base == snapshot.digest
    assert not plan.blocking and not plan.debt and not plan.affected
    if guard != "none":
        assert not plan.writes
        if guard == "disagree":
            assert counts[rule] == {"proposed": 1, "applied": 0, "withheld": 1}
            assert counts["withheld_reasons"] == {"versions disagree": 1}
        return
    assert counts[rule] == {"proposed": 1, "applied": 1, "withheld": 0}
    expected = {"prelude": (first[0], "func_80000410"),
                "split": (first[0], target[0], "func_80000410"), "merge": (first[0],)}[rule]
    assert tomllib.loads(plan.writes["layout.toml"].decode())["group"][0]["members"] == list(expected)
    for version in snapshot.versions.values():
        split = yaml.safe_load(plan.writes[version.split])["segments"][0]["subsegments"]
        assert tuple(row[2] for row in split[:-1]) == expected
        symbols = plan.writes[version.symbols_file]
        assert all(name.encode() in symbols for name in expected)
        if rule != "split":
            assert target[0].encode() not in symbols


def test_decoded_references_and_cache(scene):
    jal = (3 << 26) | ((BASE + 8) >> 2 & 0x3FFFFFF)
    snapshot = scene((("caller", "asm", (jal, 0)), ("callee", "asm", RETURN),
                      ("table", "data", (BASE + 8, 0x12345678))))
    version = snapshot.versions["a"]
    rows = layout.version_data.rows(version, snapshot.read)
    words, refs = layout._decoded(snapshot, version, rows)
    assert words == {"caller": (jal, 0), "callee": RETURN}
    assert refs == frozenset({BASE + 8})
    assert layout._decoded(snapshot, version, rows)[0] is words


@pytest.mark.parametrize("prior", [RETURN, (0x08000100, 0)])
def test_boundary_merge_withheld_after_return_or_jump(scene, prior):
    snapshot = scene((("first", "asm", prior), ("second", "asm", RETURN)))
    plan, counts = layout.boundary_plan(snapshot)
    assert not plan.writes
    assert counts["merge"]["proposed"] == 0


@pytest.mark.parametrize("guard", ["previous_c", "following_c", "segment", "gap", "referenced"])
def test_boundary_merge_guards(scene, monkeypatch, guard):
    entries = (("first", "c" if guard == "previous_c" else "asm", (0x24020001, 0)),
               ("second", "asm", RETURN), ("third", "c" if guard == "following_c" else "asm", RETURN))
    snapshot = scene(entries)
    if guard == "segment":
        versions = {key: replace(v, segments=(*v.segments, ("next", 0x1008, 0x1018, BASE + 8)))
                    for key, v in snapshot.versions.items()}
        snapshot = replace(snapshot, versions=versions)
    if guard == "gap":
        original = layout.version_data.rows

        def rows(version, reader, cache=None):
            result = original(version, reader)
            name, state, placement = result[0]
            result[0] = (name, state, replace(placement, rom_end=placement.rom_end - 4))
            return result

        monkeypatch.setattr(layout.version_data, "rows", rows)
    if guard == "referenced":
        monkeypatch.setattr(layout, "_decoded", lambda snap, version, rows: (
            {name: words for name, _, words in entries}, frozenset({BASE + 8}),
        ))
    plan, counts = layout.boundary_plan(snapshot)
    assert not plan.writes
    assert counts["merge"]["proposed"] == 0


@pytest.mark.parametrize("ending", ["\n", "\r\n", ""])
def test_edit_preserves_format_and_comments(ending):
    text = f"  - [0x1000, asm, 'named'] # keep{ending}".encode()
    edited = layout._edit(text, "named", state="c", start=0x1004, rename="renamed")
    assert edited == f"  - [0x1004, c, 'renamed'] # keep{ending}".encode()
    assert layout._edit(text, "named", drop=True) == b""


def test_edit_additions_and_nontext_refusal():
    text = b"  - [4096, asm, old] # original\n"
    result = layout._edit(text, "old", additions=((0x1008, "new"),))
    assert result == b"  - [4096, asm, old] # original\n  - [0x1008, asm, new]\n"
    with pytest.raises(Refusal) as caught:
        layout._edit(b"  - [0x1000, data, old]\n", "old", state="c")
    assert caught.value.findings[0].key == "layout.member"


@pytest.mark.parametrize("name,expected", [
    ("func_80000400", "func_80000408"), ("D_80000400_tail", "D_80000408_tail"),
    ("authored", "authored"), ("func_80000500", "func_80000500"),
])
def test_rename_only_matching_auto_addresses(name, expected):
    assert layout._rename(name, BASE, BASE + 8) == expected


def test_capture_cache_reuses_parsed_snapshot_and_invalidates_inputs(scene):
    original = scene()
    cfg = original.config
    first = layout.capture(cfg)
    second = layout.capture(cfg)
    assert second == first
    assert layout.version_data.read.call_count == 1
    path = cfg.project.root / "layout.toml"
    path.write_bytes(path.read_bytes() + b"\n")
    assert layout.capture(cfg).layout == first.layout
    assert layout.version_data.read.call_count == 2


def test_boundary_plan_is_computed_once_per_snapshot(scene, monkeypatch):
    snapshot = scene()
    calls = []
    words = {"func_80000400": RETURN}
    monkeypatch.setattr(layout, "_decoded", lambda s, v, r: calls.append(v.id) or (words, frozenset()))
    first = layout.boundary_plan(snapshot)
    count = len(calls)
    assert layout.boundary_plan(snapshot)[1] == first[1] and len(calls) == count


def test_each_project_keeps_its_own_cache_so_no_copy_reads_another_copys_sources(scene, tmp_path):
    from unbake import store
    cfg = scene().config
    other = replace(cfg, project=replace(cfg.project, root=tmp_path / "copy"))
    assert store.content(cfg).directory == cfg.project.root / ".unbake" / "cache"
    assert store.content(other).directory == tmp_path / "copy" / ".unbake" / "cache"
    assert store.cached(cfg, "capture", "k", lambda: b"one") == b"one"
    assert store.cached(other, "capture", "k", lambda: b"two") == b"two"  # the same key, the other project's entry
    assert store.cached(cfg, "capture", "k", lambda: b"never") == b"one"


HAND = [("a", 0x1000, 0x1020), ("b", 0x1020, 0x1040), ("c", 0x1040, 0x1060)]


def claim_scene(scene, monkeypatch, hand):
    snapshot = scene()
    version = snapshot.versions["a"]
    lines = "".join(f'      - [0x{s:X}, rodata, "{n}"]\n' for n, s, _ in hand)
    text = f"segments:\n  - name: m\n    subsegments:\n{lines}      - [0x{hand[-1][2]:X}]\n".encode()
    rows = [(n, "rodata", Placement("a", ".rodata", s, e, 0x800C0000 + s - 0x1000)) for n, s, e in hand]
    monkeypatch.setattr(layout.version_data, "rows", lambda v, reader, cache=None: rows)
    monkeypatch.setattr(layout.version_data, "section_of", lambda v, kind, where: ".rodata")
    return replace(snapshot, overlays={version.split: text}), version.split


@pytest.mark.parametrize("claims, expected", [
    ([(0x1000, 0x1060, "r/x")], ['[0x1000, rodata, "r/x"]']),  # every hand row is replaced by the claim
    ([(0x1008, 0x1018, "r/x")], ['[0x1000, rodata, "a"]', '[0x1008, rodata, "r/x"]',
                                 '[0x1018, rodata, "rodata/unresolved/800C0018"]', '[0x1020, rodata, "b"]',
                                 '[0x1040, rodata, "c"]']),  # inside one row: its head and its tail stay
    ([(0x1010, 0x1050, "r/x")], ['[0x1000, rodata, "a"]', '[0x1010, rodata, "r/x"]',
                                 '[0x1050, rodata, "rodata/unresolved/800C0050"]']),  # b dropped, c cut at the end
    ([(0x1008, 0x1010, "r/x"), (0x1010, 0x1020, "r/y")], ['[0x1000, rodata, "a"]', '[0x1008, rodata, "r/x"]',
                                                         '[0x1010, rodata, "r/y"]', '[0x1020, rodata, "b"]',
                                                         '[0x1040, rodata, "c"]']),  # a tail never doubles a claim
    ([(0x1020, 0x1040, "r/x")], ['[0x1000, rodata, "a"]', '[0x1020, rodata, "r/x"]', '[0x1040, rodata, "c"]']),
])
def test_claim_rows_makes_each_claim_one_row_and_names_what_it_cuts_by_address(scene, monkeypatch, claims, expected):
    snapshot, split = claim_scene(scene, monkeypatch, HAND)
    made = tuple(Claim("u", "a", ".rodata", s, e, (n,)) for s, e, n in claims)
    got = layout.claim_rows(snapshot, made)
    assert [line.strip(" -") for line in got[split].decode().splitlines()[3:-1]] == expected


def test_claim_rows_changes_nothing_where_the_rows_are_the_claims_and_refuses_a_name_in_use(scene, monkeypatch):
    snapshot, _ = claim_scene(scene, monkeypatch, HAND)
    assert layout.claim_rows(snapshot, (Claim("u", "a", ".rodata", 0x1020, 0x1040, ("b",)),)) == {}
    with pytest.raises(Refusal):
        layout.claim_rows(snapshot, (Claim("u", "a", ".rodata", 0x1000, 0x1010, ("c",)),))
    with pytest.raises(Refusal):
        layout.claim_rows(snapshot, (Claim("u", "a", ".rodata", 0x2000, 0x2010, ("z",)),))


def test_unit_options_data_member_gets_its_own_unit(scene):
    snapshot = scene((("func_80000400", "asm", RETURN), ("tbl", "rodata", (1, 2))))
    unit, writes = layout.unit_options(snapshot, "tbl", b"const int tbl[] = {1, 2};\n")[0]
    assert unit.path == "src/tbl.c" and unit.members == ("tbl",) and unit.group == "grp"
    assert writes[unit.path] == b"const int tbl[] = {1, 2};\n"
    for version in snapshot.versions.values():
        assert b", .rodata, tbl]" in writes[version.split] and b", rodata, tbl]" in snapshot.read(version.split)


def test_unit_options_data_member_refuses_compiled_state(scene):
    snapshot = scene((("func_80000400", "asm", RETURN), ("tbl", "c", RETURN)))
    with pytest.raises(Refusal) as caught:
        layout.unit_options(snapshot, "tbl", b"x")
    assert caught.value.findings[0].key == "layout.member"


def test_dump_map_writes_the_documented_layout_text():
    import tomllib
    groups = {"x": Group("x", "s", ("m", "n"), "proven", ("cap",), False)}
    units = {"a.c": UnitSpec("a.c", "c", "x", ("m",), "t", {"add": ["-O1"], "omit": []}, ("eu",)),
             "b.c": UnitSpec("b.c", "c", "x", ("n",), "t", {"add": [], "omit": []})}
    fuzzy = {"m": {"path": 'p"q', "scores": {"us-rev1": 0.5, "de": 1.0}}}
    text = layout.dump_map(LayoutMap(32, groups, {}, units, "d", (), fuzzy))
    assert text.decode() == (
        'schema = 3\ncap = 32\n\n[[group]]\nname = "x"\nsegment = "s"\nmembers = ["m", "n"]\nevidence = "proven"\n'
        'signals = ["cap"]\nsdk = false\n\n[[unit]]\npath = "a.c"\nkind = "c"\ngroup = "x"\n'
        'members = ["m"]\ntoolchain = "t"\nwithheld = ["eu"]\n\n[unit.options]\nadd = ["-O1"]\nomit = []\n\n'
        '[[unit]]\npath = "b.c"\nkind = "c"\ngroup = "x"\nmembers = ["n"]\ntoolchain = "t"\n\n[unit.options]\n'
        'add = []\nomit = []\n\n[[fuzzy]]\nmember = "m"\npath = "p\\"q"\nscores = {de = 1.0, us-rev1 = 0.5}\n')
    assert tomllib.loads(text.decode())["fuzzy"][0]["scores"] == {"de": 1.0, "us-rev1": 0.5}
    empty = tomllib.loads(layout.dump_map(LayoutMap(32, {}, {}, {}, "d", (), {})).decode())
    assert empty == {"schema": 3, "cap": 32, "group": [], "unit": []}


def _ungrouped_data(scene, entries):
    snapshot = scene(entries)
    members = {n: replace(m, group="") if m.kind != "function" else m for n, m in snapshot.layout.members.items()}
    groups = {k: replace(g, members=tuple(n for n in g.members if members[n].kind == "function"))
              for k, g in snapshot.layout.groups.items()}
    text = layout.dump_map(replace(snapshot.layout, members=members, groups=groups))
    (snapshot.config.project.root / "layout.toml").write_bytes(text)
    return layout.overlay(snapshot, {"layout.toml": text})


def _land_data(snapshot, name):
    unit, writes = layout.unit_options(snapshot, name, b"const unsigned int d[2] = {1, 2};\n")[0]
    return unit, layout.overlay(snapshot, writes)


def test_unowned_data_lands_as_a_unit_of_a_data_module_of_the_rows_that_follow_each_other(scene):
    entries = (("func_80000400", "asm", RETURN), ("rodata/unresolved/80000408", "rodata", (1, 2)),
               ("rodata/unresolved/80000410", "rodata", (3, 4)), ("rodata/unresolved/80000418", "rodata", (5, 6)))
    snapshot = _ungrouped_data(scene, entries)
    snapshot = replace(snapshot, layout=replace(snapshot.layout, cap=2))
    first, landed = _land_data(snapshot, "rodata/unresolved/80000410")
    assert first.group == "data_80000408" and first.kind == "data" and first.members == ("rodata/unresolved/80000410",)
    module = landed.layout.groups["data_80000408"]  # the run of unowned rows, cut at the cap, not the function before
    assert module.members == ("rodata/unresolved/80000408", "rodata/unresolved/80000410") and module.segment == "main"
    assert layout.unit_of(landed, "rodata/unresolved/80000410") == first
    assert b", .rodata, rodata/unresolved/80000410]" in landed.read("versions/a/Game.yaml")
    assert landed.layout.members["rodata/unresolved/80000408"].group == "data_80000408"
    second, _ = _land_data(landed, "rodata/unresolved/80000408")  # its neighbour joins the module that exists
    assert second.group == "data_80000408"
    third, more = _land_data(snapshot, "rodata/unresolved/80000418")  # the next cut is a module of its own
    assert third.group == "data_80000418" and set(more.layout.groups) == {"grp", "data_80000418"}


def test_data_named_for_its_function_joins_that_functions_module(scene):
    entries = (("func_80000400", "asm", RETURN), ("rodata/func_80000400/80000408", "rodata", (1, 2)))
    snapshot = _ungrouped_data(scene, entries)
    unit, landed = _land_data(snapshot, "rodata/func_80000400/80000408")
    assert unit.group == "grp" and landed.layout.groups == snapshot.layout.groups


def test_unit_options_keeps_the_signature_recipe_calls() -> None:
    import inspect
    assert str(inspect.signature(layout.unit_options)) == (
        "(snapshot: 'Snapshot', member: 'str', source: 'bytes') -> 'list[tuple[UnitSpec, dict[str, bytes | None]]]'")


def test_boundary_joins_the_data_rows_one_symbol_names_in_two_versions(scene):
    data = (0x11111111, 0x22222222)
    snapshot = scene((("func_80000400", "asm", RETURN), ("rodata_a", "data", data)),
                     other=(("func_80000400", "asm", RETURN), ("rodata_b", "data", data)))
    shared = {"shared": BASE + 8}
    snapshot = replace(snapshot, versions={k: replace(v, symbols=shared) for k, v in snapshot.versions.items()})
    plan, counts = layout.boundary_plan(snapshot)
    assert counts["join"] == 1
    names = {k: [r[2] for r in yaml.safe_load(plan.writes[v.split])["segments"][0]["subsegments"][:-1]]
             for k, v in snapshot.versions.items() if v.split in plan.writes}
    assert names == {"b": ["func_80000400", "rodata_a"]}
    again = layout.overlay(snapshot, {"versions/b/Game.yaml": plan.writes["versions/b/Game.yaml"]})
    assert layout.boundary_plan(again)[1]["join"] == 0


def _joined(scene, landed_names):
    data = (0x11111111, 0x22222222)
    units = tuple(_unit((n,), f"src/{n}.c", "data") for n in landed_names)
    snapshot = scene((("func_80000400", "asm", RETURN), ("rodata_a", "data", data)),
                     other=(("func_80000400", "asm", RETURN), ("rodata_b", "data", data)), units=units)
    shared = {"shared": BASE + 8}
    snapshot = replace(snapshot, versions={k: replace(v, symbols=shared) for k, v in snapshot.versions.items()})
    plan, counts = layout.boundary_plan(snapshot)
    return {k: [r[2] for r in yaml.safe_load(plan.writes[v.split])["segments"][0]["subsegments"][:-1]]
            for k, v in snapshot.versions.items() if v.split in plan.writes}, counts


def test_boundary_join_keeps_the_name_a_landed_unit_owns(scene):
    names, counts = _joined(scene, ("rodata_b",))
    assert names == {"a": ["func_80000400", "rodata_b"]} and counts["join"] == 1


def test_boundary_join_never_renames_a_row_between_two_landed_names(scene):
    names, counts = _joined(scene, ("rodata_a", "rodata_b"))
    assert names == {} and counts["join"] == 0
