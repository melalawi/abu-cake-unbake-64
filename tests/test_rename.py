"""rename: one symbol, one commit, every token-exact spelling."""

from __future__ import annotations

from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from unbake import rename, symbols
from unbake.contracts import Refusal

FILES = {"src/f.c": b"void old(void) { old(); old_x(); }\n", "include/h.h": b"extern void old(void);\n",
         "types.toml": b'"old" = { signature = "void old(void);" }\n', "src/blob.bin": b"\xff\xfe"}


@pytest.fixture
def case(monkeypatch):
    table = {"old": {"kind": "function", "a": 0x80000400}, "taken": {"kind": "data", "a": 0x80000500}}
    config = SimpleNamespace(project=SimpleNamespace(
        versions=("a",), version_files={"a": SimpleNamespace(symbols="versions/a/symbol_addrs.txt")}))
    files = {symbols.path(): symbols.dump(table), **FILES}
    snapshot = SimpleNamespace(config=config, commit="h" * 40, digest="d", read=files.__getitem__)
    applied = []
    monkeypatch.setattr(rename.effort, "stage", lambda name: nullcontext())
    monkeypatch.setattr(rename.store, "exclusive", lambda c, n: nullcontext(True))
    monkeypatch.setattr(rename.journal, "recover", lambda c: None)
    monkeypatch.setattr(rename.layout, "capture", lambda c: snapshot)
    monkeypatch.setattr(rename.layout, "overlay", lambda s, writes: s)
    monkeypatch.setattr(rename.build, "symbols_ld", lambda s, v: b"PROVIDE(new);\n")
    monkeypatch.setattr(rename.process, "git", lambda c, *a: SimpleNamespace(stdout=("\0".join(FILES) + "\0").encode()))
    monkeypatch.setattr(rename.journal, "apply", lambda c, plan, head: applied.append(plan) or "c" * 40)
    return config, applied


def test_rename_rewrites_the_table_every_token_and_the_generated_files_in_one_commit(case) -> None:
    config, applied = case
    result = rename.run(config, {"old": "old", "new": "new"})
    plan, = applied
    assert plan.message == "rename: old -> new" and result["commit"] == "c" * 40
    assert set(plan.writes) == {"symbols.toml", "versions/a/symbol_addrs.txt", "versions/a/symbols.ld", "src/f.c",
                                "include/h.h", "types.toml"}  # the unreadable file is skipped
    assert plan.writes["src/f.c"] == b"void new(void) { new(); old_x(); }\n"
    assert plan.writes["versions/a/symbol_addrs.txt"] == b"new = 0x80000400; // type:func\ntaken = 0x80000500;\n"
    assert "new" in symbols.parse(plan.writes["symbols.toml"], ("a",))
    assert result["files"] == sorted(plan.writes)


@pytest.mark.parametrize(("old", "new", "key"), [("old", "taken", "symbols.exists"), ("old", "9x", "symbols.name"),
                                                 ("missing", "new", "symbols.unknown")])
def test_rename_refuses_before_writing_anything(case, old, new, key) -> None:
    config, applied = case
    with pytest.raises(Refusal) as error:
        rename.run(config, {"old": old, "new": new})
    assert error.value.findings[0].key == key and applied == []
