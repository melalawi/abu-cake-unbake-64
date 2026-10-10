"""infer: group partition, SDK identification, cross-version correspondence and the plan."""

from __future__ import annotations

import json
import tomllib
import zlib
from dataclasses import replace
from pathlib import Path
from typing import Any

import fixture
import pytest

from unbake import config as configuration
from unbake import infer, layout, pool
from unbake.contracts import Group, Member, Refusal, Snapshot

JR_RA, NOP = 0x03E00008, 0
VRAM = fixture.VRAM_BASE


def blob(*words: int) -> bytes:
    return b"".join(w.to_bytes(4, "big") for w in words)


def jal(address: int) -> int:
    return 0x0C000000 | ((address >> 2) & 0x03FFFFFF)


# The fixture writes no `unit` table for an empty list and replaces empty groups by an authored one,
# so every project carries one asm unit and one empty inferred group (infer drops both).
UNIT = {"path": "asm/a/f0.s", "kind": "asm", "group": "u", "members": ["f0"], "toolchain": "gcc-test",
        "options": {"add": [], "omit": []}}
EMPTY = {"name": "empty", "segment": "main", "members": [], "evidence": "inferred", "signals": [],
         "sdk": False}
LEAF = (0x24020001, 0x24030002, JR_RA, NOP)
TAIL = (0x24040004, 0x24040004, JR_RA, NOP)  # the last member runs to the end of the ROM page


def vram(offset: int) -> int:
    return VRAM + offset - fixture.ROM_BASE


def both(offset: int, code: bytes) -> dict[str, tuple[int, bytes]]:
    return {v: (offset, code) for v in ("a", "b")}


def snap(tmp: Path, **kw: Any) -> Snapshot:
    kw.setdefault("groups", [EMPTY])
    cfg = fixture.config(tmp, units=[UNIT], **kw)
    for name, placed in kw.get("functions", {}).items():
        for version in placed:
            fixture._write(cfg.project.root / "asm" / version / f"{name}.s", f"glabel {name}\n")
    return layout.capture(cfg)


def chain(tmp: Path, bodies: list[tuple[int, ...]], **kw: Any) -> Snapshot:
    """Functions f0.. packed from 0x1000, each body written as given (four words keeps them 16 bytes)."""
    functions, offset = {}, 0x1000
    for i, words in enumerate(bodies):
        functions[f"f{i}"] = both(offset, blob(*words))
        offset += 4 * len(words)
    return snap(tmp, functions=functions, **kw)


def calls(i: int) -> tuple[int, ...]:
    return (jal(vram(0x1000 + 0x10 * i)), NOP, JR_RA, NOP)


def names(result: Any) -> list[tuple[str, ...]]:
    return [g.members for g in result]


@pytest.mark.usefixtures("toolchains")
def test_unsignalled_functions_pack_up_to_the_cap(tmp_path: Path) -> None:
    snapshot = chain(tmp_path, [LEAF, LEAF, LEAF])
    whole = infer.groups(snapshot)
    assert names(whole) == [("f0", "f1", "f2")] and whole[0].signals == ()
    assert whole[0].evidence == "inferred" and not whole[0].sdk
    assert whole[0].name == f"code_{vram(0x1000):08X}"
    capped = infer.groups(replace(snapshot, layout=replace(snapshot.layout, cap=2)))
    assert names(capped) == [("f0", "f1"), ("f2",)] and "cap" in capped[0].signals


@pytest.mark.usefixtures("toolchains")
def test_callee_join_is_a_signal(tmp_path: Path) -> None:
    result = infer.groups(chain(tmp_path, [calls(1), LEAF, LEAF]))
    assert names(result) == [("f0", "f1", "f2")] and result[0].signals == ("callee",)


@pytest.mark.usefixtures("toolchains")
def test_padding_cut_splits_groups_and_blocks_a_join(tmp_path: Path) -> None:
    far = (jal(vram(0x1020)), NOP, JR_RA, NOP)  # f0 calls f2 across f1
    joined = infer.groups(chain(tmp_path / "j", [far, LEAF, LEAF]))
    assert names(joined) == [("f0", "f1", "f2")] and joined[0].signals == ("callee",)
    padded = (jal(vram(0x1020)), JR_RA, NOP, NOP)  # trailing zero word beyond the delay slot
    cut = infer.groups(chain(tmp_path / "c", [padded, LEAF, LEAF]))
    assert names(cut) == [("f0",), ("f1", "f2")]
    assert cut[0].signals == ("padding",) and "callee" not in cut[1].signals


@pytest.mark.usefixtures("toolchains")
def test_authored_group_kept_and_excluded_from_inference(tmp_path: Path) -> None:
    authored = {"name": "mine", "segment": "main", "members": ["f0"], "evidence": "authored", "signals": [],
                "sdk": False}
    result = infer.groups(chain(tmp_path, [LEAF, LEAF], groups=[authored]))
    assert result[0].name == "mine" and result[0].evidence == "authored"
    assert names(result) == [("f0",), ("f1",)]
    assert result[1].evidence == "inferred" and result[1].members == ("f1",)


@pytest.mark.usefixtures("toolchains")
def test_every_function_gets_a_group_with_a_unique_name(tmp_path: Path) -> None:
    taken = {"name": f"code_{vram(0x1010):08X}", "segment": "main", "members": ["f0"], "evidence": "authored",
             "signals": [], "sdk": False}  # the name inference would give f1's run
    snapshot = chain(tmp_path, [LEAF, LEAF, LEAF], groups=[taken])
    result = infer.groups(snapshot)
    assert sorted(m for g in result for m in g.members) == ["f0", "f1", "f2"]
    assert len({g.name for g in result}) == len(result)
    planned = infer.plan(snapshot)
    value = layout.load_map(snapshot.config, snapshot.versions, planned.writes["layout.toml"], snapshot.read)
    assert [n for n, m in value.members.items() if m.kind == "function" and not m.group] == []


@pytest.mark.usefixtures("toolchains")
def test_cap_limits_group_size(tmp_path: Path) -> None:
    snapshot = chain(tmp_path, [calls(1), calls(2), calls(3), LEAF])
    capped = replace(snapshot, layout=replace(snapshot.layout, cap=2))
    result = infer.groups(capped)
    assert [m for g in result for m in g.members] == ["f0", "f1", "f2", "f3"]
    assert max(len(g.members) for g in result) <= 2
    assert len(infer.groups(snapshot)) == 1


@pytest.mark.usefixtures("toolchains")
def test_version_only_member_is_placed_by_its_version_order(tmp_path: Path) -> None:
    functions = {
        "f0": both(0x1000, blob(*LEAF)),
        "extra": {"a": (0x1010, blob(*LEAF))},
        "f1": both(0x1020, blob(*LEAF)),
    }
    snapshot = snap(tmp_path, functions=functions)
    members = [m for g in infer.groups(snapshot) for m in g.members]
    assert members.index("extra") == members.index("f0") + 1 or members.index("extra") < members.index("f1")
    assert sorted(members) == ["extra", "f0", "f1"]


# ------------------------------------------------------------------ sdk


def catalog(tmp: Path, snapshot: Snapshot, signatures: list[dict[str, Any]]) -> Snapshot:
    path = tmp / "sdk.json"
    path.write_text(json.dumps({"source": "test", "signatures": signatures}))
    host = replace(snapshot.config.host, sdk_catalog=path)
    return replace(snapshot, config=replace(snapshot.config, host=host))


def words_row(name: str, words: tuple[int, ...], masks: tuple[int, ...] | None = None) -> dict[str, Any]:
    masks = masks or (0,) * len(words)
    return {"name": name, "words": [f"{w:08x}" for w in words], "masks": [f"{m:08x}" for m in masks]}


@pytest.mark.usefixtures("toolchains")
def test_sdk_without_catalog_is_empty(tmp_path: Path) -> None:
    assert infer.sdk(chain(tmp_path, [LEAF, TAIL])) == frozenset()


@pytest.mark.usefixtures("toolchains")
def test_sdk_word_and_crc_signatures(tmp_path: Path) -> None:
    lui = (0x3C040001, 0x24030002, JR_RA, NOP)
    snapshot = chain(tmp_path, [LEAF, (0x24020001, JR_RA, NOP, NOP), lui, TAIL])
    masked = blob(0x3C040000, 0x24030000, JR_RA, NOP)
    rows = [
        words_row("leaf", LEAF),
        {"name": "lui", "size": 16, "crc_head": zlib.crc32(masked[:8]), "crc_body": zlib.crc32(masked)},
    ]
    assert infer.sdk(catalog(tmp_path, snapshot, rows)) == {"f0", "f2"}


@pytest.mark.usefixtures("toolchains")
def test_sdk_ambiguous_signature_identifies_nothing(tmp_path: Path) -> None:
    snapshot = chain(tmp_path, [LEAF, TAIL])
    row_ = words_row("leaf", LEAF)
    assert infer.sdk(catalog(tmp_path, snapshot, [row_, {**row_, "name": "twin"}])) == frozenset()


@pytest.mark.usefixtures("toolchains")
def test_sdk_mask_ignores_masked_bits(tmp_path: Path) -> None:
    snapshot = chain(tmp_path, [LEAF, TAIL])
    row_ = words_row("m", (0x24020000, *LEAF[1:]), (0x0000FFFF, 0, 0, 0))
    assert infer.sdk(catalog(tmp_path, snapshot, [row_])) == {"f0"}


# ------------------------------------------------------------------ correspondence and plan


def corresponding(tmp: Path, pairs: list[tuple[str, str]], known_b: tuple[str, ...] = ()) -> Snapshot:
    """One function per (name_a, name_b) pair: version a calls name_a, version b calls name_b."""
    functions, symbols = {}, {"a": {}, "b": {}}
    for i, (_, y) in enumerate(pairs):
        offset = 0x1000 + 0x10 * i
        functions[f"f{i}"] = {"a": (offset, blob(jal(0x80001000 + 0x10 * i), NOP, JR_RA, NOP)),
                              "b": (offset, blob(jal(0x80001100 + 0x10 * i), NOP, JR_RA, NOP))}
        symbols["a"][f"f{i}"] = symbols["b"][f"f{i}"] = vram(offset)
        symbols["b"].setdefault(y, 0x80001100 + 0x10 * i)
    for name in known_b:
        symbols["b"][name] = 0x80002000
    root = fixture.project(tmp, functions=functions, symbols=symbols, groups=[EMPTY], units=[UNIT])
    for i, (x, y) in enumerate(pairs):
        for v, target in (("a", x), ("b", y)):
            (root / "asm" / v).mkdir(parents=True, exist_ok=True)
            (root / "asm" / v / f"f{i}.s").write_text(f"glabel f{i}\n    jal {target}\n    nop\n    jr $ra\n    nop\n")
    return layout.capture(configuration.load(root, fixture.host(tmp)))


@pytest.mark.usefixtures("toolchains")
def test_correspondences_name_the_two_symbols_of_one_thing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pool, "map", lambda cfg, name, fn, items, key=None: [fn(i) for i in items])
    snapshot = corresponding(tmp_path, [("foo_a", "foo_b")])
    assert infer.correspondences(snapshot) == (("foo_a", "a", "foo_b", "b"),)


@pytest.mark.usefixtures("toolchains")
def test_correspondences_dispatch_no_job_the_second_time(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    real, dispatched = pool.map, []
    monkeypatch.setattr(pool, "map", lambda cfg, name, fn, items, key=None: real(
        cfg, name, lambda i: dispatched.append(name) or fn(i), items, key))
    monkeypatch.setattr(pool, "_in_worker", True)
    snapshot = corresponding(tmp_path, [("foo_a", "foo_b")])
    infer.correspondences(snapshot)
    dispatched.clear()
    infer.correspondences(snapshot)
    assert "infer.votes" not in dispatched


@pytest.mark.usefixtures("toolchains")
def test_plan_writes_the_layout_and_no_symbols(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pool, "map", lambda cfg, name, fn, items, key=None: [fn(i) for i in items])
    snapshot = corresponding(tmp_path, [("foo_a", "foo_b")])
    plan = infer.plan(snapshot)
    assert plan.operation == "layout" and plan.base == snapshot.digest
    assert list(plan.writes) == ["layout.toml"]  # symbols are rows of symbols.toml, never planned here
    assert plan.message == "infer: 1 groups, 0 sdk units, 0 files cut"


@pytest.mark.usefixtures("toolchains")
def test_plan_keeps_a_data_module_and_its_live_rows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pool, "map", lambda cfg, name, fn, items, key=None: [fn(i) for i in items])
    snapshot = corresponding(tmp_path, [("foo_a", "foo_b")])
    row = Member("rodata/unresolved/80001200", "data", ".rodata", "data_80001200", ())
    module = Group("data_80001200", "main", (row.name, "claimed/away"), "inferred", ("adjacent",), False)
    members = {**snapshot.layout.members, row.name: row}
    snapshot = replace(snapshot, layout=replace(snapshot.layout, members=members,
                                                groups={**snapshot.layout.groups, module.name: module}))
    kept = tomllib.loads(infer.plan(snapshot).writes["layout.toml"].decode())["group"]
    assert [g["members"] for g in kept if g["name"] == module.name] == [[row.name]]


@pytest.mark.usefixtures("toolchains")
@pytest.mark.parametrize("rounds", [0, 1, 2])
def test_plan_writes_the_rows_the_claims_give_until_they_agree(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rounds: int) -> None:
    monkeypatch.setattr(pool, "map", lambda cfg, name, fn, items, key=None: [fn(i) for i in items])
    snapshot = corresponding(tmp_path, [("foo_a", "foo_b")])
    calls, seen = [], []
    monkeypatch.setattr(infer.layout, "claim_rows", lambda snap, claims: calls.append(1) or (
        {"versions/a.yaml": b"rows"} if len(calls) <= rounds else {}))
    monkeypatch.setattr(infer.layout, "overlay", lambda snap, writes: seen.append(sorted(writes)) or snap)
    plan = infer.plan(snapshot)
    assert seen == [["layout.toml", "versions/a.yaml"]] * rounds and len(calls) == rounds + 1
    assert ("versions/a.yaml" in plan.writes) == bool(rounds) and plan.message.endswith(f"{min(rounds, 1)} files cut")


@pytest.mark.usefixtures("toolchains")
def test_plan_refuses_when_the_rows_the_claims_give_move_the_claims_again(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pool, "map", lambda cfg, name, fn, items, key=None: [fn(i) for i in items])
    snapshot = corresponding(tmp_path, [("foo_a", "foo_b")])
    monkeypatch.setattr(infer.layout, "claim_rows", lambda snap, claims: {"versions/a.yaml": b"rows"})
    monkeypatch.setattr(infer.layout, "overlay", lambda snap, writes: snap)
    with pytest.raises(Refusal) as refused:
        infer.plan(snapshot)
    assert refused.value.findings[0].key == "layout.nonconvergent"
    assert refused.value.findings[0].missing == ("versions/a.yaml",)


def masks(*functions: tuple[int, ...]) -> tuple[list[str], list[bytes]]:
    return [f"f{i}" for i in range(len(functions))], [blob(*words) for words in functions]


@pytest.mark.parametrize(
    ("left", "right", "expected"),
    [
        # unique equal code anchors, the function between them pairs by the words they share
        (((1, 2, 3, 4), (10, 11, 12, 13, 14, 15), (5, 6, 7, 8)), ((1, 2, 3, 4), (10, 11, 12, 13, 14, 99), (5, 6, 7, 8)),
         {"f0": "f0", "f1": "f1", "f2": "f2"}),
        # identical code that repeats pairs in order between the anchors
        (((1, 2, 3, 4), (9, 9, 9, 9), (9, 9, 9, 9), (5, 6, 7, 8)),
         ((1, 2, 3, 4), (9, 9, 9, 9), (9, 9, 9, 9), (5, 6, 7, 8)), {"f0": "f0", "f1": "f1", "f2": "f2", "f3": "f3"}),
        # too little in common: no pairing
        (((1, 2, 3, 4), (10, 11, 12, 13), (5, 6, 7, 8)), ((1, 2, 3, 4), (20, 21, 22, 23), (5, 6, 7, 8)),
         {"f0": "f0", "f2": "f2"}),
        # two candidates equally close: no pairing
        (((1, 2, 3, 4), (10, 11, 12, 13), (5, 6, 7, 8)),
         ((1, 2, 3, 4), (10, 11, 12, 90), (10, 11, 12, 91), (5, 6, 7, 8)), {"f0": "f0", "f2": "f3"}),
    ],
)
def test_align_pairs_by_anchors_order_and_shared_words(left: tuple, right: tuple, expected: dict) -> None:
    assert infer._align((masks(*left), masks(*right))) == expected


@pytest.mark.usefixtures("toolchains")
def test_scan_records_accesses_calls_argument_accesses_and_steps(tmp_path: Path) -> None:
    body = (0x8CAB0004, 0x24A5000C,  # lw $t3, 4($a1); addiu $a1, $a1, 0xC
            0x3C018010, 0x24300100, 0x8E080010,  # lui/addiu $s0 = 0x80100100; lw $t0, 0x10($s0)
            0x3C048011, 0x24840040,  # $a0 = 0x80110040
            jal(vram(0x1000 + 4 * 15)), 0x24060100,  # jal f1 with $a2 = 0x100 in the delay slot
            0x2610000C,  # addiu $s0, $s0, 0xC: a pointer stepped by 0xC
            0x3C018012, 0x8C290020,  # lui $at; lw $t1, 0x20($at): an access with %lo on itself
            JR_RA, NOP)
    snapshot = chain(tmp_path, [body, LEAF])
    placement = infer._placement(snapshot, snapshot.layout.members["f0"])
    (_, loads, callees, _, use), = infer._scan_job((snapshot, "a", [("f0", placement)], {vram(0x1000 + 60): "f1"}, []))
    assert callees == {"f1"} and not loads
    assert use["accesses"] == [(0x80100110, 4, "lw", "based", 0x80100100), (0x80110040, 0, "jal", "taken", None),
                               (0x80120020, 4, "lw", "abs", None)]
    assert use["calls"] == [("f1", ((0, 0x80110040), (2, 0x100)))]
    assert use["args"] == [(1, 4, 4)]
    assert sorted(use["steps"], key=repr) == [("a1", 12), (0x80100100, 12)]


@pytest.mark.usefixtures("toolchains")
def test_correspondence_votes_survive_unrelated_landing_and_recompute_one_pair(tmp_path, monkeypatch):
    snapshot = corresponding(tmp_path, [("x_a", "x_b"), ("y_a", "y_b")])
    real, ran = pool.map, []
    monkeypatch.setattr(pool, "_in_worker", True)
    def mapped(cfg, name, fn, items, key=None):
        def counted(item):
            if name == "infer.votes":
                ran.append(item[3])
            return fn(item)
        return real(cfg, name, counted, items, key)
    monkeypatch.setattr(pool, "map", mapped)
    assert infer.correspondences(snapshot) == (("x_a", "a", "x_b", "b"), ("y_a", "a", "y_b", "b"))
    assert len(ran) == 4
    ran.clear()
    assert infer.correspondences(replace(snapshot, digest="other landing", commit="other commit"))
    assert ran == []
    path = snapshot.config.project.root / "asm/a/f0.s"
    path.write_text(path.read_text().replace("x_a", "z_a"))
    updated = replace(snapshot, digest="changed assembly")
    assert ("z_a", "a", "x_b", "b") in infer.correspondences(updated)
    assert len(ran) == 2 and all(rows == [("f0", "f0")] for rows in ran)


@pytest.mark.usefixtures("toolchains")
def test_correspondence_query_submits_only_member_pairs(tmp_path, monkeypatch):
    snapshot = corresponding(tmp_path, [("x_a", "x_b"), ("y_a", "y_b")])
    seen = []
    def mapped(cfg, name, fn, items, key=None):
        if name == "infer.votes":
            seen.extend(items)
        return [fn(item) for item in items]
    monkeypatch.setattr(pool, "map", mapped)
    assert infer.correspondence(snapshot, "f0") == (("x_a", "a", "x_b", "b"),)
    assert len(seen) == 2 and all(job[3] == [("f0", "f0")] for job in seen)


def test_shape_matches_mnemonics_calls_reference_order_and_literal_immediates():
    text = "/* 0 0 0 */ lui $a0, %hi(first)\n/* 4 4 4 */ lw $a0, %lo(first)($a0)\n"
    text += "/* 8 8 8 */ jal callee\n/* C C C */ addiu $a1, $zero, 8\n"
    shape = infer._shape(text)
    assert shape == infer._shape(text.replace("first", "other"))
    assert shape != infer._shape(text.replace("callee", "different"))
    assert shape != infer._shape(text.replace(", 8", ", 12"))
    assert shape != infer._shape(text.replace(" lw ", " sw "))
    assert infer._shape("/* 0 0 0 */ nop\n") == ""


@pytest.mark.usefixtures("toolchains")
def test_scan_retains_mask_and_shape(tmp_path, monkeypatch):
    monkeypatch.setattr(pool, "map", lambda cfg, name, fn, items, key=None: [fn(i) for i in items])
    snapshot = corresponding(tmp_path, [("x_a", "x_b")])
    for version, rows in infer.scan(snapshot).items():
        assert rows[0][4]["shape"] == infer._shape(infer._asm(snapshot, version, "f0").decode())
        assert isinstance(rows[0][4]["mask"], bytes)
