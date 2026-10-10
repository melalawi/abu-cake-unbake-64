"""Compiler recipes: resolve base, project, unit and override flags into one digest-named Recipe."""
from __future__ import annotations

from collections.abc import Sequence

from unbake import adapters
from unbake.contracts import Config, Finding, Json, Recipe, Refusal, UnitSpec, digest

_KNOWN = frozenset({"toolchain", "add", "omit"})
def _groups(tokens: Sequence[str], paired: Sequence[str]) -> list[tuple[str, ...]]:
    out: list[tuple[str, ...]] = []
    index = 0
    while index < len(tokens):
        take = 2 if tokens[index] in paired and index + 1 < len(tokens) else 1
        out.append(tuple(tokens[index : index + take]))
        index += take
    return out
def _words(items: Sequence[str]) -> list[str]:
    return [word for item in items for word in item.split()]
def _omit(lists: list[list[tuple[str, ...]]], omitted: Sequence[str]) -> list[list[tuple[str, ...]]]:
    for text in omitted:
        if not any(text in (g[0], " ".join(g)) for groups in lists for g in groups):
            raise Refusal(Finding("recipe.option", f"cannot omit {text}: not in the compiler flags"))
        lists = [[g for g in groups if text not in (g[0], " ".join(g))] for groups in lists]
    return lists
def _make(toolchain: str, config: Config, cppflags: Sequence[str], cflags: Sequence[str]) -> Recipe:
    build = config.project.build
    cppflags = (*cppflags, f"-D__UNBAKE_STDARG_{adapters.row(toolchain)['family'].upper()}")
    asflags = tuple(build["asflags"]) + tuple(build["gnu_asflags"])
    flags = tuple(cflags)
    return Recipe(toolchain, cppflags, flags, asflags, digest((toolchain, cppflags, flags, asflags)))
def resolve(config: Config, unit: UnitSpec, overrides: Json) -> Recipe:
    for key in overrides:
        if key not in _KNOWN:
            raise Refusal(Finding("recipe.option", f"unknown override {key}", unit=unit.path))
    toolchain = overrides.get("toolchain") or unit.toolchain
    row = adapters.row(toolchain)
    paired = tuple(row["paired"])
    supported = set(_words(row["supported_options"]))
    adds = list(overrides.get("add", ()))
    omits = list(overrides.get("omit", ()))
    for text in adds:
        if text in omits:
            raise Refusal(Finding("recipe.conflict", f"{text} is both added and omitted", unit=unit.path))
    base = [*row["cflags"], *config.project.build["cflags"], *unit.options.get("add", ())]
    groups, pre = _omit([_groups(_words(base), paired), _groups(config.project.build["cppflags"], paired)],
                        unit.options.get("omit", ()))
    groups, pre = _omit([groups, pre], omits)
    preprocessor = tuple(row["preprocessor_options"])
    present = {word for g in groups for word in g}
    for text in adds:
        words = text.split()
        if not words or (words[0] not in supported and words[0] not in present
                         and not words[0].startswith(preprocessor)):
            raise Refusal(Finding("recipe.option", f"cannot add {text}: not supported by {toolchain}", unit=unit.path))
        groups.extend(_groups(words, paired))
    moved = [g for g in groups if g[0].startswith(preprocessor)]
    return _make(toolchain, config, [w for g in (*pre, *moved) for w in g],
                 [w for g in groups if g not in moved for w in g])
