"""Type evidence tests with all native and worker boundaries mocked."""

import tomllib
from contextlib import contextmanager
from dataclasses import replace
from unittest.mock import Mock

import pytest
import tomlkit

from unbake import types
from unbake.contracts import (
    Config,
    Finding,
    Host,
    LayoutMap,
    Member,
    Placement,
    Project,
    Refusal,
    Snapshot,
    SourceView,
    UnitSpec,
)

AGREED: dict = {}  # what headers.disagreements answers: name -> the one type its declarations agree on
CONFLICTS: list = []
HEADER_DECLARATIONS: dict = {}  # what headers.catalog answers: name -> (path, line, statement)


def _document(**tables):
    if "global_" in tables:
        tables["global"] = tables.pop("global_")
    return {"schema": 1, "function": {}, "global": {}, "struct": {}, **tables}


def _with_doc(snapshot, doc):
    return replace(snapshot, overlays={**snapshot.overlays, "types.toml": tomlkit.dumps(doc).encode()})


def _member(name, kind="function", holders=("a", "b"), section=".text", size=8):
    placements = tuple(
        Placement(v, section, 0x1000, 0x1000 if section == ".bss" else 0x1000 + size,
                  0x80000400, size if section == ".bss" else 0)
        for v in holders
    )
    return Member(name, kind, "asm" if kind == "function" else "data", "group", placements)


@pytest.fixture
def lane(tmp_path, monkeypatch):
    host = Host(2, 2, 1024, 1024, 1024, tmp_path / "tools",
                {}, None, 2, 2, 0.8, 2, ("test", "test@invalid"), {}, "host")
    project = Project(tmp_path, "test", "test", "Test", ("a", "b"), "a", "test-tc",
                      {}, {}, {}, {}, 200, {}, "project")
    cfg = Config(project, host, "config")
    members = {name: _member(name) for name in ("alpha", "zeta")}
    members["buffer"] = _member("buffer", "data", section=".data", size=0x18)
    snapshot = Snapshot(cfg, "head", LayoutMap(200, {}, members, {}, "layout", (), {}), {}, {}, "snapshot")
    overlays = {f"asm/{v}/{name}.s": f"glabel {name}\n nop\n".encode()
                for name in ("alpha", "zeta") for v in ("a", "b")}
    snapshot = replace(_with_doc(snapshot, _document()), overlays={
        **_with_doc(snapshot, _document()).overlays, **overlays, "include/h.h": b"header",
    })
    span = Mock()

    @contextmanager
    def stage(_name):
        yield span

    cache = {}

    def cached(config, kind, key, produce):
        assert config is cfg
        if (kind, key) not in cache:
            cache[kind, key] = produce()
        return cache[kind, key]

    resources = {"units.toml": {"kind": {"c": {"decompiled": True}, "asm": {"decompiled": False}}}}
    monkeypatch.setattr(types.effort, "stage", stage)
    monkeypatch.setattr(types.store, "cached", cached)
    monkeypatch.setattr(types.headers, "sources",
                        lambda snap, place="include", suffix=".h": ["include/h.h"] * (place == "include"))
    monkeypatch.setattr(types.headers, "disagreements", lambda snap, defined=(): (AGREED, CONFLICTS))
    monkeypatch.setattr(types.headers, "catalog", lambda snap, version: HEADER_DECLARATIONS)
    HEADER_DECLARATIONS.clear()
    AGREED.clear()
    CONFLICTS.clear()
    monkeypatch.setattr(types.config, "load_resource", Mock(side_effect=resources.__getitem__))
    return (snapshot,)


@pytest.mark.parametrize("holders,section,size", [
    (("a", "b"), ".data", 0x18), (("b",), ".bss", 0x21),
])
def test_scan_builds_globals(lane, holders, section, size):
    snapshot, = lane
    members = {"alpha": _member("alpha", holders=holders),
               "buffer": _member("buffer", "data", holders, section, size),
               "pad_padding_0": _member("pad_padding_0", "data", holders, section, 4)}
    snapshot = replace(snapshot, layout=replace(snapshot.layout, members=members))
    doc = tomllib.loads(types.scan(snapshot).decode())
    assert doc["function"] == {} and doc["struct"] == {}
    assert "pad_padding_0" not in doc["global"]  # alignment filler is not a global
    assert doc["global"]["buffer"] == {
        "declaration": f"extern u8 buffer[0x{size:X}];", "section": section, "size": size, "evidence": "splat",
    }


def test_scan_takes_the_type_the_landed_declarations_agree_on_and_only_in_types_it_knows(lane):
    snapshot, *_ = lane
    HEADER_DECLARATIONS["Word"] = ("include/h.h", 1, "typedef int Word;")
    AGREED.update({"buffer": "const Word @[4]", "alpha": "void @(Word)", "zeta": "Local @(int)"})
    members = {"buffer": _member("buffer", "data", section=".data", size=0x18), "alpha": _member("alpha"),
               "zeta": _member("zeta")}
    snapshot = replace(snapshot, layout=replace(snapshot.layout, members=members))
    doc = tomllib.loads(types.scan(snapshot).decode())
    assert doc["global"]["buffer"] == {"declaration": "extern const Word buffer[4];", "section": ".data",
                                       "size": 0x18, "evidence": "declared"}
    assert doc["function"]["alpha"] == {"signature": "void alpha(Word)", "evidence": "declared"}
    assert "zeta" not in doc["function"]  # Local is a type no header gives the type map


def test_conflicts_are_the_findings_of_the_names_the_declarations_disagree_on(lane):
    snapshot, *_ = lane
    CONFLICTS.append("finding")
    assert types.conflicts(_with_doc(snapshot, _document())) == ["finding"]


def test_scan_keeps_landed_functions_and_authored_globals_only(lane):
    snapshot, *_ = lane
    kept = _document(
        function={"alpha": {"signature": "void alpha(void)", "evidence": "landed"}},
        global_={"buffer": {"declaration": "extern int buffer;", "section": ".data", "size": 4,
                            "evidence": "authored"}},
    )
    old = {**kept, "function": {**kept["function"], "zeta": {"signature": "void s(void)", "evidence": "m2c"}},
           "struct": {"Stale": {"declaration": "struct Stale { int a; };", "evidence": "authored"}}}
    doc = tomllib.loads(types.scan(_with_doc(snapshot, old)).decode())
    assert doc["function"] == kept["function"] and doc["global"]["buffer"] == kept["global"]["buffer"]
    assert doc["struct"] == {}  # types come from the headers, not from the previous map


def test_scan_records_the_headers_declarations_as_authored(lane):
    snapshot, *_ = lane
    HEADER_DECLARATIONS.update({
        "struct Pair": ("include/h.h", 3, "struct Pair { int a; int b; };"),
        "Word": ("include/h.h", 4, "typedef int Word;"),
        "gCount": ("include/h.h", 5, "extern int gCount;"),
        "handler": ("include/h.h", 6, "extern void (*handler)(int a);"),
        "known": ("include/h.h", 7, "extern int known(int a, void *b);"),
        "bare": ("include/h.h", 8, "void bare(void);"),
    })
    landed = {"known": {"signature": "s32 known(s32 a, void *b)", "evidence": "landed"}}
    doc = tomllib.loads(types.scan(_with_doc(snapshot, _document(function=landed))).decode())
    assert doc["struct"] == {"Pair": {"declaration": "struct Pair { int a; int b; };", "evidence": "authored"},
                             "Word": {"declaration": "typedef int Word;", "evidence": "authored"}}
    assert doc["function"] == {"known": landed["known"],  # a landed signature outranks the header's
                               "bare": {"signature": "void bare(void)", "evidence": "authored"}}


def test_scan_deterministic(lane):
    snapshot, *_ = lane
    first = types.scan(snapshot)
    reordered = replace(snapshot, layout=replace(snapshot.layout,
                                                members=dict(reversed(list(snapshot.layout.members.items())))))
    assert types.scan(reordered) == first


@pytest.mark.parametrize("marker", ["src/alpha.c", "./src/alpha.c", "absolute", "build/views/abcd/src/alpha.c"])
def test_landed_replaces_signature_and_keeps_authored_types(lane, marker):
    snapshot, *_ = lane
    authored = {"declaration": "struct Known { int a; };", "evidence": "authored"}
    snapshot = _with_doc(snapshot, _document(function={"alpha": {"signature": "int alpha(void)", "evidence": "m2c"}},
                                             struct={"Known": authored}))
    if marker == "absolute":
        marker = (snapshot.config.project.root / "src/alpha.c").as_posix()
    unit = UnitSpec("src/alpha.c", "c", "group", ("alpha",), "test-tc", {})
    text = f'# 1 "include/header.h"\nint header(void) {{ return 0; }}\n#line 1 "{marker}"\n'
    text += 'void alpha(int value) { }\n#line 1 "include/tail.h"\nint tail(void) { return 0; }\n'
    view = SourceView(unit.path, "a", "key", text, (), ())
    doc = tomllib.loads(types.landed(snapshot, unit, view).decode())
    assert doc["function"] == {"alpha": {"signature": "void alpha(int value)", "evidence": "landed"}}
    assert doc["struct"] == {"Known": authored}


def test_landed_parse_refusal_names_the_view_line_and_its_file(lane):
    snapshot, *_ = lane
    unit = UnitSpec("src/alpha.c", "c", "group", ("alpha",), "test-tc", {})
    text = '# 1 "include/bad.h" 1\nint ok;\nextern foo;\n# 2 "src/alpha.c" 2\nvoid alpha(void) { }\n'
    with pytest.raises(Refusal) as refused:
        types.landed(snapshot, unit, SourceView(unit.path, "a", "key", text, (), ()))
    finding = refused.value.findings[0]
    assert finding.key == "headers.parse" and "include/bad.h" in finding.reason and "extern foo;" in finding.reason

def test_landed_reads_the_c_the_way_the_fold_does(lane):
    snapshot, *_ = lane
    snapshot = _with_doc(snapshot, _document())
    unit = UnitSpec("src/alpha.c", "c", "group", ("alpha",), "test-tc", {})
    text = 'typedef struct __attribute__((packed)) { char a; int b; } P;\nvoid alpha(P *p) { }\n'
    view = SourceView(unit.path, "a", "key", text, (), ())
    doc = tomllib.loads(types.landed(snapshot, unit, view).decode())
    assert doc["function"]["alpha"]["evidence"] == "landed"


def test_load_missing_refuses(lane):
    snapshot, *_ = lane
    snapshot = replace(snapshot, overlays={"types.toml": None})
    with pytest.raises(Refusal) as caught:
        types.load(snapshot)
    assert caught.value.findings == (Finding("config.missing", "types.toml does not exist",
                                            path="types.toml", action="unbake setup"),)


def test_load_validates(lane, monkeypatch):
    snapshot, *_ = lane
    validate = Mock()
    monkeypatch.setattr(types.config, "validate", validate)
    assert types.load(snapshot) == _document()
    validate.assert_called_once_with("types", _document(), "types.toml")


def test_types_toml_is_parsed_once_per_command(lane, monkeypatch):
    from unbake import effort
    snapshot = lane[0] if isinstance(lane, tuple) else lane
    parses = []
    real = types.config.tomllib.loads
    monkeypatch.setattr(types.config.tomllib, "loads", lambda text: parses.append(1) or real(text))
    with effort.command("check", []):
        assert types.load(snapshot) is types.load(snapshot)
    assert len(parses) == 1
