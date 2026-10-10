"""recipes: flag precedence, paired groups, refusals, proposals and digests."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from unbake import recipes
from unbake.contracts import Config, Refusal, UnitSpec, digest

PAIRED_ROW = {
    "cflags": ["-O2", "-G", "0"],
    "supported_options": ["-O1", "-O2", "-O3", "-mgp32", "-G"],
    "paired": ["-G"],
}


def _config(cfg: Config, **build: list[str]) -> Config:
    merged = {**cfg.project.build, **build}
    return replace(cfg, project=replace(cfg.project, build=merged))


def _unit(path: str = "src/a.c", toolchain: str = "gcc-test", **options: list[str]) -> UnitSpec:
    return UnitSpec(path, "c", "g", ("f",), toolchain, options)


@pytest.fixture
def paired(toolchains: dict[str, Any]) -> str:
    toolchains["toolchain"]["gcc-paired"] = {**toolchains["toolchain"]["gcc-test"], **PAIRED_ROW}
    return "gcc-paired"


def test_precedence_order(cfg: Config, toolchains: dict[str, Any]) -> None:
    row = toolchains["toolchain"]["gcc-test"]
    toolchains["toolchain"]["gcc-test"] = {**row, "supported_options": ["-O1", "-O2", "-Wall", "-g"]}
    cfg = _config(cfg, cflags=["-P1", "-P2"])
    unit = _unit(add=["-U1", "-U2"], omit=["-P1"])
    recipe = recipes.resolve(cfg, unit, {"omit": ["-U1", "-O2"], "add": ["-Wall", "-g"]})
    assert recipe.cflags == ("-P2", "-U2", "-Wall", "-g")
    assert recipe.toolchain == "gcc-test"


def test_asflags_and_cppflags_from_build(cfg: Config) -> None:
    cfg = _config(cfg, cppflags=["-DX"], asflags=["-a"], gnu_asflags=["-g1"])
    recipe = recipes.resolve(cfg, _unit(), {})
    assert recipe.cppflags == ("-DX", "-D__UNBAKE_STDARG_GCC")
    assert recipe.asflags == ("-a", "-g1")
    assert recipe.digest == digest((recipe.toolchain, recipe.cppflags, recipe.cflags, recipe.asflags))


def test_toolchain_override(cfg: Config) -> None:
    recipe = recipes.resolve(cfg, _unit(), {"toolchain": "ido-7.1"})
    assert recipe.toolchain == "ido-7.1"
    assert "-non_shared" in recipe.cflags


def test_paired_omit(cfg: Config, paired: str) -> None:
    recipe = recipes.resolve(cfg, _unit(toolchain=paired), {"omit": ["-G"]})
    assert recipe.cflags == ("-O2",)
    recipe = recipes.resolve(cfg, _unit(toolchain=paired), {"omit": ["-G 0"]})
    assert recipe.cflags == ("-O2",)


def test_paired_add(cfg: Config, paired: str) -> None:
    recipe = recipes.resolve(cfg, _unit(toolchain=paired), {"omit": ["-G"], "add": ["-G 8"]})
    assert recipe.cflags == ("-O2", "-G", "8")


@pytest.mark.parametrize(
    ("overrides", "key"),
    [
        ({"bogus": 1}, "recipe.option"),
        ({"omit": ["-O3"]}, "recipe.option"),
        ({"add": ["-nonsense"]}, "recipe.option"),
        ({"toolchain": "nope"}, "adapter.unknown"),
    ],
)
def test_unknown_option_refused(cfg: Config, overrides: dict[str, Any], key: str) -> None:
    with pytest.raises(Refusal) as caught:
        recipes.resolve(cfg, _unit(), overrides)
    assert caught.value.findings[0].key == key


def test_conflict_refused(cfg: Config) -> None:
    with pytest.raises(Refusal) as caught:
        recipes.resolve(cfg, _unit(), {"add": ["-O1"], "omit": ["-O1"]})
    assert caught.value.findings[0].key == "recipe.conflict"


def test_unit_omit_missing_refused(cfg: Config) -> None:
    with pytest.raises(Refusal) as caught:
        recipes.resolve(cfg, _unit(omit=["-O3"]), {})
    assert caught.value.findings[0].key == "recipe.option"


def test_digest_changes_only_with_flags(cfg: Config) -> None:
    one = recipes.resolve(cfg, _unit("src/a.c"), {})
    two = recipes.resolve(cfg, replace(_unit("src/b.c"), group="other", members=("x", "y")), {})
    assert one.digest == two.digest
    changed = recipes.resolve(cfg, _unit(), {"omit": ["-O2"], "add": ["-O1"]})
    assert changed.digest != one.digest
