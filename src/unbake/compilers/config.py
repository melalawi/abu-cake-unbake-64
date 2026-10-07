"""Compiler-owned compatibility for persisted build configuration."""

from typing import Any

BUILD_ALIASES = {"sn64_asflags": "gnu_asflags"}
BUILD_KEYS = frozenset({"asflags", "cppflags", "gnu_asflags", "resident_mappings", *BUILD_ALIASES})


def build_values(table: dict[str, Any], label: str) -> dict[str, Any]:
    from unbake.config import Held

    result = dict(table)
    for old, new in BUILD_ALIASES.items():
        if old in result:
            if new in result and result[new] != result[old]:
                raise Held("config", f"{label} [build].{new}: conflicts with {old}")
            result[new] = result.pop(old)
    return result
