"""A warm command reads, resolves and parses each thing once: the tests count work, never time."""
from __future__ import annotations

import os
import tomllib
from unittest.mock import Mock

from unbake import config as configuration
from unbake import effort, layout, pool, store, versions, view
from unbake.contracts import digest


def test_digest_keeps_its_canonical_text() -> None:
    assert digest({"b": (1, 2), "a": [b"x"]}) == digest({"a": [b"x"], "b": [1, 2]})
    assert digest({"s": {3, 1, 2}}) == digest({"s": [1, 2, 3]})


def test_a_header_shared_by_two_units_is_read_and_resolved_once(tmp_path, monkeypatch) -> None:
    (tmp_path / "include").mkdir()
    (tmp_path / "include/shared.h").write_text("int x;\n")
    for name in ("a.c", "b.c"):
        (tmp_path / name).write_text('#include "shared.h"\n')
    include = (str(tmp_path / "include"),)
    reads = []
    real = view._INCLUDE.findall
    monkeypatch.setattr(view, "_INCLUDE", Mock(findall=lambda data: reads.append(data) or real(data)))
    first, second = (view._reached(str(tmp_path / n), include) for n in ("a.c", "b.c"))
    assert first == second == {str(tmp_path / "include/shared.h")}
    assert len(reads) == 3  # a.c, b.c and shared.h, each once


def test_a_pin_is_hashed_once_until_store_write_replaces_the_file(cfg) -> None:
    path = cfg.project.root / "include/p.h"
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(b"one")
    snapshot = Mock(overlays={}, config=cfg)
    before = view._pin(snapshot, "include/p.h")
    path.write_bytes(b"two")
    assert view._pin(snapshot, "include/p.h") == before  # remembered for the command
    store.write(path, b"three")
    assert view._pin(snapshot, "include/p.h") != before


def test_a_validated_table_is_parsed_once_across_commands(cfg, monkeypatch) -> None:
    parses = []
    real = tomllib.loads
    monkeypatch.setattr(configuration.tomllib, "loads", lambda text: parses.append(1) or real(text))
    data = b"schema = 1\n\n[symbol]\n"
    for _ in range(2):
        effort._memo.clear()
        configuration.toml("symbols", data, "symbols.toml", "symbols.table", store.content(cfg).cached)
    assert len(parses) == 1


def test_a_group_warm_item_by_item_is_sealed_for_the_next_run(cfg, monkeypatch) -> None:
    items = [1, 2, 3]
    for index in (str, str):
        pool.map(cfg, "units", abs, items, key=index)
    reads = []
    real = pool.store.get
    monkeypatch.setattr(pool.store, "get", lambda c, kind, key: reads.append(kind) or real(c, kind, key))
    pool.map(cfg, "units", abs, items, key=str)
    assert reads == ["units.group"]


def test_a_listing_is_read_once_until_a_stream_is_appended(cfg, monkeypatch) -> None:
    (cfg.project.root / ".unbake/attempts").mkdir(parents=True)
    (cfg.project.root / ".unbake/attempts/m.jsonl").write_text("")
    listed = []
    real = os.listdir
    monkeypatch.setattr(os, "listdir", lambda p: listed.append(p) or real(p))
    assert "m.jsonl" in store.listing(cfg, "attempts") and "m.jsonl" in store.listing(cfg, "attempts")
    assert len(listed) == 1
    store.append(cfg, "attempts/n", {"a": 1})
    assert "n.jsonl" in store.listing(cfg, "attempts") and len(listed) == 2


def test_the_capture_pins_follow_contents_not_modification_times(cfg) -> None:
    root = cfg.project.root
    (root / "f.txt").write_text("same")
    first = layout._pins(cfg, ["f.txt"])
    os.utime(root / "f.txt", ns=(1, 1))
    assert layout._pins(cfg, ["f.txt"]) == first  # rewritten identically: still warm
    (root / "f.txt").write_text("other")
    assert layout._pins(cfg, ["f.txt"]) != first


def test_a_split_file_is_parsed_once_across_commands(cfg) -> None:
    parses = []
    def parse(data: bytes) -> list:
        parses.append(1)
        return [data]
    cached = store.content(cfg).cached
    for _ in range(2):
        effort._memo.clear()
        assert versions._parsed("document", b"x", parse, cached) == [b"x"]
    assert len(parses) == 1
