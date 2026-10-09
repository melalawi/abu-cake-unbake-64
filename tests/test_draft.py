"""draft: hints matching, m2c function drafts and word/byte data drafts. m2c is mocked through process.run."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import fixture
import pytest

from unbake import config as configuration
from unbake import draft, headers, layout, process
from unbake.contracts import Refusal, Snapshot

CODE = bytes.fromhex("03e0000800000000")


def _words(*values: int) -> bytes:
    return b"".join(v.to_bytes(4, "big") for v in values)


def _retype(root: Path, kinds: dict[str, str]) -> None:
    """Rewrite Game.yaml subsegment types by member name (the fixture only writes asm and data)."""
    for path in root.glob("versions/*/Game.yaml"):
        document = json.loads(path.read_text())
        for sub in document["segments"][0]["subsegments"]:
            if len(sub) == 3 and sub[2] in kinds:
                sub[1] = kinds[sub[2]]
        path.write_text(json.dumps(document, indent=1) + "\n")


def _snapshot(tmp: Path, kinds: dict[str, str] | None = None, **kw: Any) -> Snapshot:
    code = {"func_80000400": {"a": (0x1000, CODE), "b": (0x1000, CODE)}}
    kw.setdefault("functions", code)
    members = [*kw["functions"], *kw.get("data", {})]
    group = {"name": "code_80000400", "segment": "main", "members": members, "evidence": "authored"}
    group |= {"signals": [], "subsystem": "unknown", "sdk": False}
    kw.setdefault("groups", [group])
    root = fixture.project(tmp, **kw)
    if kinds:
        _retype(root, kinds)
    loaded = configuration.load(root, fixture.host(tmp))
    return layout.capture(loaded)


def _data_snapshot(tmp: Path, blob: bytes, kinds: dict[str, str] | None = None) -> Snapshot:
    data = {"d": {"a": (0x1200, blob)}, "e": {"a": (0x1200 + len(blob), b"\0\0\0\0")}}
    return _snapshot(tmp, kinds, data=data, versions=("a",))


def _mock_m2c(monkeypatch: pytest.MonkeyPatch, stdout: bytes, exit: int = 0, stderr: bytes = b"") -> list[Any]:
    calls: list[Any] = []
    monkeypatch.setattr(headers, "context", lambda snapshot, version: Path("context.c"))

    def run(name: str, argv: Any, cwd: Path, **kw: Any) -> Any:
        calls.append((name, list(argv)))
        return fixture.native_result(argv, exit, stdout, stderr)

    monkeypatch.setattr(process, "run", run)
    return calls


def _base(tmp: Path) -> Snapshot:
    return _snapshot(tmp)


def _with_hints(snapshot: Snapshot, text: str) -> Snapshot:
    return layout.overlay(snapshot, {"hints.jsonl": text.encode()})


def test_hints_match_and_because(tmp_path: Path, toolchains: Any) -> None:
    snapshot = _base(tmp_path)
    matched, subsystem = draft.hints(snapshot, "unknown", {"register_dominant": True})
    row = next(m for m in matched if m["id"] == "register-priority")
    assert row["because"] == {"register_dominant": True}
    assert all(m["id"] != "register-priority" for m in subsystem)


def test_hints_missing_symptom_never_matches(tmp_path: Path, toolchains: Any) -> None:
    matched, subsystem = draft.hints(_base(tmp_path), "unknown", {"score": 0.5})
    assert all(m["id"] != "register-priority" for m in matched)
    assert any(s["id"] == "register-priority" and s["because"] == {} for s in subsystem)


def test_hints_empty_match_only_subsystem_rows(tmp_path: Path, toolchains: Any) -> None:
    matched, subsystem = draft.hints(_base(tmp_path), "unknown", {"register_dominant": True, "score": 0.1})
    assert "hoisted-invariant-address" not in {m["id"] for m in matched}
    assert "hoisted-invariant-address" in {s["id"] for s in subsystem}
    cap = configuration.load_resource("flow.toml")["packet"]["hints"]
    assert len(subsystem) <= cap


@pytest.mark.parametrize(("symptoms", "expected"), [({"score": 0.2}, True), ({"score": 0.4}, False)])
def test_project_hints_appended(tmp_path: Path, toolchains: Any, symptoms: dict[str, float], expected: bool) -> None:
    row = {"id": "mine", "subsystem": "any", "detect": "d", "match": {"score": {"lt": 0.3}}, "technique": "t"}
    row |= {"example": "x", "scope": "ordinary", "qualifier_effect": "none"}
    snapshot = _with_hints(_base(tmp_path), "\n" + json.dumps(row) + "\n\n")
    matched, _ = draft.hints(snapshot, "unknown", symptoms)
    assert ("mine" in {m["id"] for m in matched}) is expected


def test_hint_row_invalid_refuses_config_schema(tmp_path: Path, toolchains: Any) -> None:
    snapshot = _with_hints(_base(tmp_path), json.dumps({"id": "bad"}) + "\n")
    with pytest.raises(Refusal) as error:
        draft.hints(snapshot, "unknown", {})
    assert error.value.findings[0].key == "config.schema"
    assert "hints.jsonl:1" in str(error.value.findings[0].path)


def test_function_draft_writes_m2c_output(tmp_path: Path, toolchains: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    snapshot = _snapshot(tmp_path)
    asm = tmp_path / "project/asm/a/func_80000400.s"
    asm.parent.mkdir(parents=True)
    asm.write_text("glabel func_80000400\n")
    calls = _mock_m2c(monkeypatch, b"void func_80000400(void) {\n    gDPPipeSync(gfx++);\n}\n")
    monkeypatch.setattr(draft.types, "declarations", lambda s, names: {})
    out = tmp_path / "out" / "draft.c"
    result = draft.create(snapshot, "func_80000400", out)
    assert out.read_text()
    assert calls[0][0] == "m2c" and "--valid-syntax" in calls[0][1] and "--stack-structs" in calls[0][1]
    assert result["kind"] == "function" and result["version"] == "a" and result["subsystem"] == "unknown"
    assert result["gbi_rewrites"] == draft.gbi.rewrite(out.read_text())[1] or result["gbi_rewrites"] >= 0
    configuration.validate("result.draft", result, "result")


def test_function_draft_declares_the_target_and_its_callees(tmp_path: Path, toolchains: Any,
                                                            monkeypatch: pytest.MonkeyPatch) -> None:
    snapshot = _snapshot(tmp_path)
    asm = tmp_path / "project/asm/a/func_80000400.s"
    asm.parent.mkdir(parents=True)
    asm.write_text("glabel func_80000400\n/* 0 0 0 */ jal func_80000500\n/* 4 4 4 */ jal func_80000400\n")
    _mock_m2c(monkeypatch, b"void func_80000400(void) {\n}\n")
    seen = []
    monkeypatch.setattr(draft.types, "declarations", lambda s, names: seen.append(names) or {
        "func_80000400": "void func_80000400(void);", "func_80000500": "s32 func_80000500(s32 arg0);"})
    out = tmp_path / "out" / "draft.c"
    result = draft.create(snapshot, "func_80000400", out)
    assert seen == [["func_80000400", "func_80000500"]]
    assert out.read_text().startswith("s32 func_80000500(s32 arg0);\nvoid func_80000400")
    assert result["declarations"] == 2


def test_function_draft_m2c_failure_refuses(tmp_path: Path, toolchains: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    snapshot = _snapshot(tmp_path)
    asm = tmp_path / "project/asm/a/func_80000400.s"
    asm.parent.mkdir(parents=True)
    asm.write_text("glabel func_80000400\n")
    _mock_m2c(monkeypatch, b"", exit=1, stderr=b"boom\n")
    with pytest.raises(Refusal) as error:
        draft.create(snapshot, "func_80000400", tmp_path / "o.c")
    assert error.value.findings[0].key == "draft.m2c"


def test_function_draft_m2c_failure_names_what_m2c_printed(
        tmp_path: Path, toolchains: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    snapshot = _snapshot(tmp_path)
    asm = tmp_path / "project/asm/a/func_80000400.s"
    asm.parent.mkdir(parents=True)
    asm.write_text("glabel func_80000400\n")
    printed = b"/*\nDecompilation failure in function f:\n\nLast instruction is missing a delay slot:\nj .L1\n*/\n"
    _mock_m2c(monkeypatch, printed, exit=1, stderr=b"")
    with pytest.raises(Refusal) as error:
        draft.create(snapshot, "func_80000400", tmp_path / "o.c")
    assert "missing a delay slot: j .L1" in error.value.findings[0].reason


def test_unknown_member_refuses_land_request(tmp_path: Path, toolchains: Any) -> None:
    with pytest.raises(Refusal) as error:
        draft.create(_base(tmp_path), "nope", tmp_path / "o.c")
    assert error.value.findings[0].key == "land.request"


def test_data_draft_words_symbols_and_rodata_const(tmp_path: Path, toolchains: Any) -> None:
    blob = _words(0x80000400, 0x80000610, 0x12345678, 0x80000600)  # function f, data e, plain, itself
    snapshot = _data_snapshot(tmp_path, blob, {"d": "rodata"})
    out = tmp_path / "out" / "d.c"
    result = draft.create(snapshot, "d", out)
    text = out.read_text()
    assert "extern void func_80000400(void);" in text
    assert "extern unsigned char e[];" in text
    assert text.index("extern") < text.index("const unsigned int d[] = {")
    assert "func_80000400, &e, 0x12345678, 0x80000600," in text
    assert result["kind"] == "data" and result["gbi_rewrites"] == 0
    configuration.validate("result.draft", result, "result")


def test_data_draft_non_rodata_has_no_const(tmp_path: Path, toolchains: Any) -> None:
    snapshot = _data_snapshot(tmp_path, _words(1, 2))
    out = tmp_path / "d.c"
    draft.create(snapshot, "d", out)
    assert out.read_text() == "unsigned int d[] = {\n    0x00000001, 0x00000002,\n};\n"
    assert "const" not in out.read_text()


def test_data_draft_bytes_when_unaligned(tmp_path: Path, toolchains: Any) -> None:
    snapshot = _data_snapshot(tmp_path, bytes(range(10)))
    out = tmp_path / "d.c"
    draft.create(snapshot, "d", out)
    text = out.read_text()
    assert "unsigned char d[] = {" in text
    assert "0x00, 0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07,\n    0x08, 0x09," in text


def test_data_draft_names_the_object_by_its_symbol_not_its_member_path(tmp_path: Path, toolchains: Any) -> None:
    snapshot = _data_snapshot(tmp_path, _words(1))
    member = replace(snapshot.layout.members["d"], name="rodata/f/80000600")
    snapshot = replace(snapshot, layout=replace(snapshot.layout, members={**snapshot.layout.members,
                                                                         "rodata/f/80000600": member}))
    out = tmp_path / "named.c"
    draft.create(snapshot, "rodata/f/80000600", out)
    assert "unsigned int d[] = {" in out.read_text()  # the symbol at its address
    record = replace(snapshot.versions["a"], symbols={"func_80000400": 0x80000400})
    draft.create(replace(snapshot, versions={"a": record}), "rodata/f/80000600", out)
    assert "unsigned int D_80000600[] = {" in out.read_text()  # no symbol there: a C name from the address


def test_bss_only_refuses_draft_data(tmp_path: Path, toolchains: Any) -> None:
    snapshot = _data_snapshot(tmp_path, _words(0, 0), {"d": "bss"})
    with pytest.raises(Refusal) as error:
        draft.create(snapshot, "d", tmp_path / "d.c")
    assert error.value.findings[0].key == "draft.data"


def test_run_writes_under_work(tmp_path: Path, toolchains: Any) -> None:
    snapshot = _data_snapshot(tmp_path, _words(1))
    result = draft.run(snapshot.config, {"item": "d"})
    assert Path(result["path"]) == snapshot.config.project.root / ".unbake" / "work" / "d.c"
    assert Path(result["path"]).is_file()
