"""Compiler-owned compatibility for persisted build configuration."""

import tomllib
from importlib.resources import files
from typing import Any

BUILD_ALIASES = tomllib.loads(files("unbake.compilers").joinpath("registry.toml").read_text())["build_aliases"]
BUILD_KEYS = frozenset({"asflags", "cppflags", "gnu_asflags", "resident_mappings", *BUILD_ALIASES})


def build_values(table: dict[str, Any], label: str) -> dict[str, Any]:
    from unbake.config import Held
    from unbake.process import named as cause_named

    result = dict(table)
    for old, new in BUILD_ALIASES.items():
        if old in result:
            if new in result and result[new] != result[old]:
                raise Held(
                    cause_named(
                        "compilers.config.build_values",
                        f"{label} [build].{new}: conflicts with {old}",
                        owner="compilers.config",
                        stage="config",
                    )
                )
            result[new] = result.pop(old)
    return result
