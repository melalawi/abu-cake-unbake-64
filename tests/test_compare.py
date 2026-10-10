"""compare: holders, gaps, bind, measure and run with native, pool and land mocked."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import fixture
import pytest

from unbake import compare, ownership
from unbake import config as configuration
from unbake.contracts import Config, LayoutMap, Member, Placement, Recipe, Refusal, Snapshot, UnitSpec

RECIPE = Recipe("gcc-test", (), (), (), "recipe")
UNIT = "src/a.c"


@pytest.fixture(autouse=True)
def _no_ownership(monkeypatch):  # the units here have no source to compile: they own what they list
    monkeypatch.setattr(ownership, "derive", lambda snapshot, unit: (snapshot, unit))


def member(name: str, versions: tuple[str, ...] = ("a", "b")) -> Member:
    places = tuple(Placement(v, ".text", 0x1000, 0x1008, 0x80000400) for v in versions)
    return Member(name, "function", "c", "g", places)


def unit(*names: str, path: str = UNIT) -> UnitSpec:
    return UnitSpec(path, "c", "g", tuple(names), "gcc-test", {})


def snap(config: Config, members: dict[str, Member], units: tuple[UnitSpec, ...] = ()) -> Snapshot:
    layout = LayoutMap(1, {}, members, {u.path: u for u in units}, "d", (), {})
    return Snapshot(config, "c" * 40, layout, {}, {}, "s")


def test_holders_union_sorted(cfg: Config) -> None:
    s = snap(cfg, {"f": member("f", ("b",)), "g": member("g", ("a", "c"))})
    assert compare.holders(s, unit("f", "g")) == ("a", "b", "c")


@pytest.mark.parametrize("exact_b", [True, False])
def test_gaps_per_member(cfg: Config, exact_b: bool) -> None:
    s = snap(cfg, {"f": member("f")})
    proofs = [fixture.proof(UNIT, "f", "a", True)]
    proofs.append(fixture.proof(UNIT, "f", "b", exact_b, () if exact_b else ("bytes differ",)))
    found = compare.gaps(s, unit("f"), proofs)
    assert bool(found) is (not exact_b)
    if found:
        assert found[0].key == "land.not_exact"
        assert found[0].versions == ("b",)


def test_gap_missing_measurement(cfg: Config) -> None:
    s = snap(cfg, {"f": member("f")})
    found = compare.gaps(s, unit("f"), [fixture.proof(UNIT, "f", "a", True)])
    assert found[0].versions == ("b",)
    assert found[0].missing == ("version b: no measurement",)
    assert found[0].symptoms == {"no_measurement": True}


def test_gap_symptoms_merged(cfg: Config) -> None:
    s = snap(cfg, {"f": member("f")})
    bad = fixture.proof(UNIT, "f", "a", False, ("size 8 != 12",), symptoms={"register_dominant": True})
    found = compare.gaps(s, unit("f"), [bad])
    facts = found[0].symptoms
    assert facts["register_dominant"] is True  # from the proof
    assert facts["size_delta_bytes"] == -4  # from the missing text
    assert facts["no_measurement"] is True  # version b was never measured


def test_bind_known_unit(cfg: Config) -> None:
    u = unit("f", path="src/a.c")
    s = snap(cfg, {"f": member("f")}, (u,))
    file = cfg.project.root / "src/a.c"
    assert compare.bind(s, file, None) == (u, s)


def test_bind_new_text_for_a_landed_member_replaces_its_unit_file(cfg: Config, tmp_path: Path,
                                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    u = unit("f", "g", path="src/a.c")
    s = snap(cfg, {"f": member("f"), "g": member("g")}, (u,))
    file = tmp_path / "f.c"
    file.write_bytes(b"int f;")
    over = replace(s, digest="overlaid")
    writes = []
    monkeypatch.setattr(compare.layout, "overlay", lambda snapshot, w: writes.append(w) or over)
    monkeypatch.setattr(compare.layout, "unit_options", lambda *a: pytest.fail("a landed member needs no new unit"))
    assert compare.bind(s, file, None) == (u, over)
    assert writes == [{"src/a.c": b"int f;"}]
def test_bind_missing_file_refused_by_name(cfg: Config, tmp_path: Path) -> None:
    s = snap(cfg, {"f": member("f")})
    with pytest.raises(Refusal) as error:
        compare.bind(s, tmp_path / "absent.c", "f")
    assert error.value.findings[0].key == "land.request" and "absent.c" in error.value.findings[0].reason


def test_bind_unknown_member_refused(cfg: Config) -> None:
    s = snap(cfg, {"f": member("f")})
    with pytest.raises(Refusal) as error:
        compare.bind(s, Path("/elsewhere/nope.c"), None)
    assert error.value.findings[0].key == "land.request"


def test_bind_function_names_member_and_overlays(cfg: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    s = snap(cfg, {"f": member("f")})
    file = tmp_path / "draft.c"
    file.write_bytes(b"int f;")
    u = unit("f")
    over = replace(s, digest="overlaid")
    monkeypatch.setattr(compare.layout, "unit_options", lambda snapshot, name, source: [(None, {}), (u, {"x": b"1"})])
    monkeypatch.setattr(compare.layout, "overlay", lambda snapshot, writes: over)
    assert compare.bind(s, file, "f") == (u, over)


def test_bind_no_options_refused(cfg: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    s = snap(cfg, {"f": member("f")})
    file = tmp_path / "f.c"
    file.write_bytes(b"")
    monkeypatch.setattr(compare.layout, "unit_options", lambda *a: [])
    with pytest.raises(Refusal) as error:
        compare.bind(s, file, None)
    assert "no standalone unit option" in error.value.findings[0].reason


def mock_measure(monkeypatch: pytest.MonkeyPatch, seen: list[Path], exact: bool = True) -> None:
    monkeypatch.setattr(compare.recipes, "resolve", lambda *a: RECIPE)

    def fake_map(config: Config, name: str, function: Any, items: list[Any]) -> list[Any]:
        seen.extend(item[4] for item in items)
        return [(fixture.proof(UNIT, "f", item[2], exact, () if exact else ("bytes differ",)),) for item in items]

    monkeypatch.setattr(compare.pool, "map", fake_map)


def test_measure_refuses_existing_out(cfg: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    s = snap(cfg, {"f": member("f")})
    out = tmp_path / "out"
    out.mkdir()
    with pytest.raises(Refusal) as error:
        compare.measure(s, unit("f"), {"add": [], "omit": []}, out)
    assert error.value.findings[0].key == "compare.out"


def test_work_dir_removed_after_measure(cfg: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[Path] = []
    mock_measure(monkeypatch, seen)
    s = snap(cfg, {"f": member("f")})
    proofs = compare.measure(s, unit("f"), {"add": [], "omit": []}, None)
    assert [p.version for p in proofs] == ["a", "b"]
    assert [p.name for p in seen] == ["a", "b"]  # per-holder job dirs under one work dir
    assert seen[0].parent == seen[1].parent
    assert not seen[0].parent.exists()


def test_measure_exports_before_work_removed(cfg: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    mock_measure(monkeypatch, [])
    s = snap(cfg, {"f": member("f")})
    exported: list[Path] = []

    def export(snapshot: Snapshot, u: UnitSpec, version: str, recipe: Recipe, work: Path,
               proofs: Any, target: Path) -> None:
        exported.append(work.parent)
        assert work.parent.exists()
        target.mkdir(parents=True)
        (target / "result.json").write_text("{}")

    monkeypatch.setattr(compare, "_export", export)
    out = tmp_path / "out"
    compare.measure(s, unit("f"), {"add": [], "omit": []}, out)
    assert sorted(p.name for p in out.iterdir()) == ["a", "b"]
    assert not (out.parent / ".tmp-out").exists()
    assert not exported[0].exists()


def mock_run(monkeypatch: pytest.MonkeyPatch, cfg: Config, u: UnitSpec, exact: bool,
             submitted: Any, calls: dict[str, Any]) -> Snapshot:
    s = snap(cfg, {n: member(n) for n in u.members}, (u,))
    seen: list[Path] = []
    mock_measure(monkeypatch, seen, exact)
    monkeypatch.setattr(compare.layout, "capture", lambda config: s)
    monkeypatch.setattr(compare, "bind", lambda snapshot, file, function: (u, snapshot))
    def fake_submit(config: Config, request: dict[str, Any], spec: UnitSpec, proofs: Any,
                    source: bytes, origin: str) -> Any:
        calls["submit"] = (request, origin, source)
        return submitted

    def fake_drain(config: Config) -> dict[str, Any]:
        calls["drain"] = calls.get("drain", 0) + 1
        return {"running": False, "landed": [], "refused": []}

    monkeypatch.setattr(compare.land, "submit", fake_submit)
    monkeypatch.setattr(compare.land, "drain", fake_drain)
    return s


def params(tmp_path: Path, **over: Any) -> dict[str, Any]:
    file = tmp_path / "f.c"
    file.write_bytes(b"int f;")
    base = {"file": file, "function": None, "toolchain": None, "flag": ["-g"], "omit_flag": [], "out": None}
    base["note"] = None
    return base | over


def validate(result: dict[str, Any]) -> None:
    configuration.validate("result.compare", json.loads(json.dumps(result)), "result.compare")


def test_run_exact_submits_and_drains(cfg: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: dict[str, Any] = {}
    mock_run(monkeypatch, cfg, unit("f"), True, fixture.submission(id="sub1"), calls)
    result = compare.run(cfg, params(tmp_path, note="trick"))
    assert result["submitted"] == "sub1"
    assert calls["drain"] == 1
    assert result["drain"] == {"running": False, "landed": [], "refused": []}
    assert result["exact"] is True
    assert result["score"] == {"a": 1.0, "b": 1.0}
    request, origin, source = calls["submit"]
    assert origin == "compare" and source == b"int f;"
    assert request["function"] == "f" and request["note"] == "trick"
    assert request["overrides"] == {"add": ["-g"], "omit": []}


def test_run_not_exact_submits_nothing(cfg: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: dict[str, Any] = {}
    mock_run(monkeypatch, cfg, unit("f"), False, None, calls)
    result = compare.run(cfg, params(tmp_path, toolchain="gcc-test"))
    assert "drain" not in calls
    assert result["submitted"] is None and result["drain"] is None
    assert result["exact"] is False
    assert result["gaps"]
    assert calls["submit"][0]["overrides"]["toolchain"] == "gcc-test"


@pytest.mark.parametrize("exact,submitted", [(True, "sub"), (False, None)])
def test_run_result_validates_schema(cfg: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                     exact: bool, submitted: str | None) -> None:
    sub = fixture.submission(id=submitted) if submitted else None
    mock_run(monkeypatch, cfg, unit("f"), exact, sub, {})
    validate(compare.run(cfg, params(tmp_path)))


def test_multi_member_file_needs_function(cfg: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: dict[str, Any] = {}
    mock_run(monkeypatch, cfg, unit("f", "g"), True, None, calls)
    with pytest.raises(Refusal) as error:
        compare.run(cfg, params(tmp_path))
    assert error.value.findings[0].key == "land.request"
    assert "--function" in error.value.findings[0].reason
    result = compare.run(cfg, params(tmp_path, function="g"))
    assert calls["submit"][0]["function"] == "g"
    assert result["members"] == ["f", "g"]



def test_run_audits_the_derived_claim_rows(cfg: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    original = unit("f", "old_row")
    calls: dict[str, Any] = {}
    mock_run(monkeypatch, cfg, original, True, None, calls)
    claimed = unit("f", "rodata/f/00001000")
    after = snap(cfg, {"f": member("f"), "rodata/f/00001000": member("rodata/f/00001000", ("a",))}, (claimed,))
    monkeypatch.setattr(ownership, "derive", lambda snapshot, spec: (after, claimed))
    proofs = [fixture.proof(UNIT, "f", v, True) for v in ("a", "b")]
    proofs.append(fixture.proof(UNIT, "rodata/f/00001000", "a", True))
    monkeypatch.setattr(compare, "measure", lambda snapshot, spec, overrides, out: tuple(proofs))
    result = compare.run(cfg, params(tmp_path, function="f"))
    assert result["exact"] and result["gaps"] == []
    assert result["members"] == list(claimed.members)


def test_gaps_require_only_the_claim_rows_holders(cfg: Config) -> None:
    row = member("rodata/f/00001000", ("b",))
    snapshot = snap(cfg, {"f": member("f"), row.name: row})
    proofs = [fixture.proof(UNIT, "f", v, True) for v in ("a", "b")]
    assert compare.gaps(snapshot, unit("f", row.name), proofs)[0].versions == ("b",)
    proofs.append(fixture.proof(UNIT, row.name, "b", True))
    assert compare.gaps(snapshot, unit("f", row.name), proofs) == ()


def test_measure_many_runs_all_variants_in_one_pool_map(cfg: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[Path] = []
    calls: list[int] = []
    mock_measure(monkeypatch, seen)
    inner = compare.pool.map
    monkeypatch.setattr(compare.pool, "map", lambda *a: calls.append(len(a[3])) or inner(*a))
    s = snap(cfg, {"f": member("f")})
    proofs = compare.measure_many(s, unit("f"), [{"add": [], "omit": []}, {"add": ["-O1"], "omit": []}])
    assert calls == [4] and [[p.version for p in each] for each in proofs] == [["a", "b"], ["a", "b"]]
    assert len({p.parent / p.name for p in seen}) == 4  # each variant has its own job directory
