"""Shared pytest fixtures. The fake toolchain row replaces toolchains.toml only; every other resource loads for real."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import fixture
import pytest
from abucache.store import forget

from unbake import config as configuration
from unbake import effort, pool
from unbake.contracts import Config

TOOLCHAIN_ROW = {
    "family": "gcc",
    "cc": "cc",
    "as_host": "mips_as",
    "cflags": ["-O2"],
    "supported_options": ["-O1", "-O2"],
    "splat": "splat",
    "emits_asm": False,
    "preserves_padding": True,
    "paired": [],
    "preprocessor_options": ["-D", "-U", "-I"],
    "small_data": [8],
    "isa": [3],
    "pins": {},
    "downloads": [],
    "preprocess": ["{cpp}", "-E", "{cppflags}", "{defines}", "{includes}", "{source}"],
    "compile": ["{cc}", "{codegen}", "-c", "{source}", "-o", "{out}"],
}


def _ido_row() -> dict[str, Any]:
    """The shipped ido-7.1 row without pins or downloads, so tests need no installed toolchain."""
    row = dict(configuration.load_resource("toolchains.toml")["toolchain"]["ido-7.1"])
    return {**row, "pins": {}, "downloads": []}


@pytest.fixture
def toolchains(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Inject the toolchain rows gcc-test and ido-7.1 (no pins, no downloads) into config.load_resource."""
    real = configuration.load_resource
    # Fresh copies per test: tests may edit rows without leaking into later tests.
    document = {"schema": 1, "toolchain": {"gcc-test": copy.deepcopy(TOOLCHAIN_ROW), "ido-7.1": _ido_row()}}

    def load_resource(name: str) -> Any:
        return document if name == "toolchains.toml" else real(name)

    monkeypatch.setattr(configuration, "load_resource", load_resource)
    return document


@pytest.fixture
def host_file(tmp_path: Path) -> Path:
    return fixture.host(tmp_path)


@pytest.fixture
def cfg(tmp_path: Path, toolchains: dict[str, Any]) -> Config:
    """A loaded two-version config with one function."""
    return fixture.config(tmp_path)


@pytest.fixture(autouse=True)
def _fresh_module_state():
    """Every test starts with empty process-wide caches, so xdist workers never share state between tests."""
    forget()
    effort._memo.clear()
    effort._listeners.clear()
    pool._drop_executor()
    yield
