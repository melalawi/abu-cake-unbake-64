"""Policy contract checks without native tools, workers, or project setup."""

from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

import pytest

from unbake import policy
from unbake.contracts import (
    Config,
    Finding,
    Group,
    LayoutMap,
    Member,
    Placement,
    Project,
    Recipe,
    Snapshot,
    SourceView,
    UnitSpec,
)

PATH = "src/unit.c"


@pytest.fixture
def snapshot(monkeypatch):
    monkeypatch.setattr(policy.effort, "stage", Mock(side_effect=lambda name: nullcontext()))
    result = Mock(spec=Snapshot)
    result.config = Mock(spec=Config)
    result.config.project = Mock(spec=Project)
    result.config.project.names_from = "a"
    return result


def _source(text="", dependencies=()):
    return SourceView(PATH, "a", "view-key", text, dependencies, ())


@pytest.mark.parametrize("address", [
    "(0xA0000000 | 0x04600010)", "0x04000000", "0x04FFFFFF",
    "0xA4000010UL", "83886080", "0x1FC00000", "0xBFCFFFFF",
])
def test_hardware_cast_allowed(snapshot, monkeypatch, address):
    text = f"*(volatile unsigned int *){address} = x;"
    source = _source(text)
    active = Mock(return_value={7: text})
    monkeypatch.setattr(policy._view, "active_lines", active)
    assert policy.evaluate(snapshot, PATH, "\n" * 6 + "IO_WRITE(reg, x);", source, False) == ()
    active.assert_called_once_with(source, PATH)


@pytest.mark.parametrize("with_view", [False, True])
def test_ram_cast_and_local_denied(snapshot, monkeypatch, with_view):
    text = "(volatile int *)0x80100000;\nvolatile s32 x;"
    source = _source(text) if with_view else None
    monkeypatch.setattr(policy._view, "active_lines", Mock(return_value=dict(enumerate(text.splitlines(), 1))))
    findings = policy.evaluate(snapshot, PATH, text, source, False)
    sentence = next(r["sentence"] for r in policy.config.load_resource("rules.toml")["rule"]
                    if r["id"] == "volatile-storage")
    assert findings == tuple(Finding("source.volatile-storage", f"{sentence}: {line}", path=PATH, line=n)
                             for n, line in enumerate(text.splitlines(), 1))


@pytest.mark.parametrize("text", [
    "volatile int x;", "void f(volatile int x) {}", "struct S { volatile int x; };",
    "void f(void) { volatile int x; }", "*(volatile int *)0x80100000 = 1;",
    "*(volatile int *)0x03FFFFFF = 1;", "*(volatile int *)0x06000000 = 1;",
    "*(volatile int *)address = 1;",
])
def test_volatile_nonhardware_forms_denied(snapshot, text):
    findings = policy.evaluate(snapshot, PATH, text, None, False)
    assert [(f.key, f.line) for f in findings] == [("source.volatile-storage", 1)]


@pytest.mark.parametrize("with_view", [False, True])
def test_sdk_group_exempt(snapshot, monkeypatch, with_view):
    text = "(volatile int *)0x80100000;\nvolatile s32 x;"
    source = _source(text) if with_view else None
    monkeypatch.setattr(policy._view, "active_lines", Mock(side_effect=AssertionError("SDK needs no volatile scan")))
    assert policy.evaluate(snapshot, PATH, text, source, True) == ()


@pytest.mark.parametrize(("waiver", "waived"), [
    ("/* FAKEMATCH: timing */", True), ("/* FAKEMATCH: */", False),
    ("/* FAKEMATCH:   */", False), ("// FAKEMATCH: timing", False),
])
@pytest.mark.parametrize("previous_line", [False, True])
def test_fakematch_waives_only_waivable(snapshot, waiver, waived, previous_line):
    code = 'while (ready); asm("nop");'
    text = waiver + "\n" + code if previous_line else code + " " + waiver
    findings = policy.evaluate(snapshot, PATH, text, None, False)
    expected = {"source.inline-asm"} if waived else {"source.inline-asm", "source.empty-loop"}
    assert {f.key for f in findings} == expected
    assert all(f.line == (2 if previous_line else 1) for f in findings)


@pytest.mark.parametrize(("code", "lines"), [
    ("do {\n  x++;\n} while (x < 4);", []),
    ("do { if (a) { b(); } } while (c);", []),
    ("if (a) {\n  b();\n}\nwhile (ready);", [4]),
    ("do { b(); } while (c);\nwhile (ready);", [2]),
    ("do {\n x++;\n}\nwhile (x < 3);", []),
    ("while (x < 3);", [1]),
    ("for (;;);", [1]),
])
def test_empty_loop_ignores_do_while_tails(snapshot, code, lines):
    findings = policy.evaluate(snapshot, PATH, code, None, False)
    assert [f.line for f in findings if f.key == "source.empty-loop"] == lines


def test_distant_fakematch_does_not_waive(snapshot):
    findings = policy.evaluate(snapshot, PATH, "/* FAKEMATCH: timing */\n\nwhile (ready);", None, False)
    assert [(f.key, f.line) for f in findings] == [("source.empty-loop", 3)]


@pytest.mark.parametrize("writes", [{PATH, "include/shared.h"}, [PATH, "include/shared.h"]])
def test_scope_new_vs_preexisting(writes):
    old = Finding("source.volatile-storage", "old qualifier", path="include/shared.h", line=1)
    moved = replace(old, line=9)
    new = Finding(old.key, "new qualifier", path=PATH, line=4, blocking=False)
    unwritten = Finding(old.key, "other qualifier", path="src/other.c", line=2)
    blocking, debt = policy.scope((old,), (moved, new, unwritten), writes)
    assert blocking == (replace(new, blocking=True),)
    assert debt == (replace(moved, blocking=False), replace(unwritten, blocking=False))
    assert new.blocking is False and moved.blocking is True


def test_scope_key_includes_rule_path_and_reason():
    old = Finding("source.empty-loop", "same line", path=PATH)
    other_rule = replace(old, key="source.inline-asm")
    other_path = replace(old, path="include/shared.h")
    assert policy.scope((old,), (other_rule, other_path), {PATH, other_path.path}) == (
        (other_rule, other_path), (),
    )
    assert policy.scope((), (), ()) == ((), ())


@pytest.mark.parametrize("text", [
    "/* volatile int x; */", "// volatile int x;", 'char *s = "volatile int x;";',
    "int c = 'volatile';", 'char *s = "escaped \\\" volatile";',
    "/* multiline\nvolatile\n*/", 'char *s = "/* volatile */ // asm( )";',
])
def test_comment_and_string_blanking(snapshot, text):
    assert policy.evaluate(snapshot, PATH, text, None, False) == ()


def test_scope_blanking_preserves_lines(snapshot):
    text = ('/* volatile\n */\nchar *s = "volatile";\nvolatile int x;\n'
            '#define SAFE "volatile"\n// Generated by tool\n')
    findings = policy.evaluate(snapshot, PATH, text, None, False)
    assert {(f.key, f.line) for f in findings} == {
        ("source.volatile-storage", 4), ("source.local-define", 5), ("source.tool-comment", 6),
    }


def test_directive_scope_and_continuations(snapshot):
    text = ('/* #include "../no.h" */\n#include /* allowed comment */ "../bad.h"\n'
            '#if VERSION_A\n#define X \\\nvolatile int x;\n#endif\n')
    assert {(f.key, f.line) for f in policy.evaluate(snapshot, PATH, text, None, False)} == {
        ("source.local-include", 2), ("source.file-version-guard", 3), ("source.local-define", 4),
    }


def test_regex_deduplicates_match_line_and_truncates_reason(snapshot):
    text = '  asm("one"); asm("two"); ' + "x" * 160
    findings = policy.evaluate(snapshot, PATH, text, None, False)
    assert findings == (Finding("source.inline-asm", "inline assembly is never allowed: " + text.strip()[:120],
                                path=PATH, line=1),)


@pytest.mark.parametrize(("text", "denied"), [
    ("dl->words.w0 = 0xE7000000;", True), ("w0 = (0xE7000000);", True),
    ("Gfx list[] = { {0xE7000000, 0x00000000} };", True),
    ("gDPWord(dl, 0xE7000000, 0x00000000);", True),
    ("unsigned int value = 0xE7000000;", False), ("w0 = 0x12000000;", False),
    ("w0 = 0xE700;", False), ('char *s = "w0 = 0xE7000000";', False),
])
def test_raw_gfx_context(snapshot, text, denied):
    findings = policy.evaluate(snapshot, PATH, text, None, False)
    assert [(f.key, f.line) for f in findings] == ([("source.raw-gfx", 1)] if denied else [])


def test_shared_declarations_active_headers_only(snapshot, monkeypatch):
    text = "extern int shared;\nint call(int x);\ntypedef int Number;\nextern int private;"
    source = _source(dependencies=((PATH, "s"), ("include/shared.h", "h"), ("src/other.c", "o")))
    active = {
        PATH: dict(enumerate(text.splitlines(), 1)),
        "include/shared.h": {1: "extern int shared;", 3: "int call(int x);", 7: "typedef int Number;"},
        "src/other.c": {1: "extern int private;"},
    }
    monkeypatch.setattr(policy._view, "active_lines", Mock(side_effect=lambda view, path: active[path]))
    assert [(f.key, f.line) for f in policy.evaluate(snapshot, PATH, text, source, False)] == [
        ("source.shared-declarations", 1), ("source.shared-declarations", 2),
        ("source.shared-declarations", 3),
    ]
    assert policy.evaluate(snapshot, PATH, text, None, False) == ()


@pytest.mark.parametrize(("names_from", "expected_version"), [("a", "a"), ("missing", "a")])
def test_census_units_headers_deduplication_and_sdk(snapshot, monkeypatch, names_from, expected_version):
    snapshot.config.project.names_from = names_from
    units = [UnitSpec(path, kind, "group", ("member",), "test", {}) for path, kind in [
        (PATH, "c"), ("src/second.c", "c"), ("src/asm.s", "asm"), ("src/data.c", "data"),
    ]]
    member = Member("member", "function", "c", "group", (
        Placement("b", ".text", 0, 4, 0), Placement("a", ".text", 0, 4, 0),
    ))
    group = Group("group", "main", ("member",), "authored", (), True)
    snapshot.layout = LayoutMap(200, {"group": group}, {"member": member},
                                {u.path: u for u in units}, "layout", (), {})
    snapshot.read.side_effect = lambda path: f"text of {path}".encode()
    recipe = Recipe("test", (), (), (), "recipe")
    resolve = Mock(return_value=recipe)
    get = Mock(side_effect=lambda snap, unit, version, rec: replace(
        _source(dependencies=((unit.path, "s"), ("include/shared.h", "h"))), unit=unit.path, version=version))
    closure = Mock(side_effect=lambda snap, unit, version: (
        (unit.path, "s"), ("include/shared.h", "h"), ("include/shared.h", "h"), ("note.txt", "n")))
    evaluate = Mock(side_effect=lambda snap, path, text, source, sdk: (
        Finding("source.volatile-storage", f"debt {path}", path=path, line=2),))
    gathered = Mock(side_effect=lambda cfg, groups: [[g[1](item) for item in g[2]] for g in groups])
    monkeypatch.setattr(policy.native, "warnings", lambda *a: ())
    monkeypatch.setattr(policy.store, "work", lambda cfg: nullcontext(Path("work")))
    monkeypatch.setattr(policy.recipes, "resolve", resolve)
    monkeypatch.setattr(policy._view, "get", get)
    monkeypatch.setattr(policy._view, "closure", closure)
    monkeypatch.setattr(policy._view, "active_lines", lambda view, path: {})
    monkeypatch.setattr(policy, "evaluate", evaluate)
    monkeypatch.setattr(policy.pool, "gather", gathered)
    monkeypatch.setattr(policy.store, "cached", lambda cfg, kind, key, produce: produce())
    monkeypatch.setattr(policy.types, "conflicts", lambda snap: [])
    findings = policy.census(snapshot)
    assert [(f.path, f.line, f.blocking) for f in findings] == [
        (PATH, 2, False), ("src/second.c", 2, False), ("include/shared.h", 2, False),
    ]
    (call,) = gathered.call_args_list  # units and headers are one dispatch, each resolving its own warm items
    assert [g[0] for g in call.args[1]] == ["policy.census", "policy.census.headers", "policy.census.warnings"]
    assert call.args[1][0][2] == [(snapshot, u) for u in units[:2]]
    assert call.args[1][1][2] == [(snapshot, "include/shared.h", True)]  # read once, not once per unit
    assert all(g[3] is not None for g in call.args[1])
    assert [call.args for call in resolve.call_args_list] == [(snapshot.config, u, {}) for u in units[:2]] * 2
    assert [call.args for call in get.call_args_list] == [(snapshot, u, expected_version, recipe) for u in units[:2]]
    assert [call.args[1] for call in evaluate.call_args_list] == [PATH, "src/second.c", "include/shared.h"]
    assert evaluate.call_args_list[-1].args[3] is None  # a header has no single view
    assert all(call.args[0] is snapshot and call.args[4] is True for call in evaluate.call_args_list)
    assert all(call.args[2] == f"text of {call.args[1]}" for call in evaluate.call_args_list)


def test_directive_rules_never_let_whitespace_cross_a_line():
    import re
    import tomllib
    from pathlib import Path
    rules = tomllib.loads((Path(policy.__file__).parent / "resources/data/rules.toml").read_text())["rule"]
    directive = [r["regex"] for r in rules if r.get("regex", "").startswith("^")]
    assert directive and not any(r"^\s" in regex for regex in directive)  # a blank-line run is not rescanned per line
    assert re.search(directive[0], "\n\n\n  #  include \"../x.h\"\n", re.M)


def test_census_counts_a_name_declared_with_two_spellings(snapshot, monkeypatch):
    conflict = Finding("types.conflict", "gX is declared 2 ways and defined nowhere", unit="gX",
                       missing=("gX: src/a.c:3 s32 gX;", "gX: include/a.h:9 f32 gX;"))
    snapshot.layout = LayoutMap(200, {}, {}, {}, "layout", (), {})
    monkeypatch.setattr(policy.pool, "gather", lambda cfg, groups: [[], [], []])
    monkeypatch.setattr(policy.types, "conflicts", lambda snap: [conflict])
    assert [(f.key, f.unit, f.blocking) for f in policy.census(snapshot)] == [("types.conflict", "gX", False)]


def test_census_counts_landed_view_conversions_as_debt_by_unit(snapshot, monkeypatch):
    unit = UnitSpec(PATH, "c", "group", ("member",), "test", {})
    snapshot.layout = LayoutMap(200, {}, {"member": Member("member", "function", "c", "group", (
        Placement("a", ".text", 0, 4, 0),))}, {unit.path: unit}, "layout", (), {})
    monkeypatch.setattr(policy.recipes, "resolve", lambda *a: None)
    monkeypatch.setattr(policy.native, "warnings", lambda *a: ("a.c:1: makes pointer from integer", "a.c:2: x"))
    monkeypatch.setattr(policy.store, "work", lambda cfg: nullcontext(Path("work")))
    found = policy._census_warnings((snapshot, unit))
    assert [(f.key, f.path, len(f.missing)) for f in found] == [("land.view-conversion", PATH, 2)]
