"""Compiler recipes: resolve base, project, unit and override flags into one digest-named Recipe."""
from __future__ import annotations

from collections.abc import Sequence

from unbake import config as config_module
from unbake.contracts import Config, Finding, Json, Recipe, Refusal, UnitSpec, digest

_KNOWN = frozenset({"toolchain", "add", "omit"})
_LEVELS = ("-O1", "-O2", "-O3")
def _row(toolchain: str) -> Json:
    rows = config_module.load_resource("toolchains.toml")["toolchain"]
    if toolchain not in rows:
        raise Refusal(Finding("adapter.unknown", f"toolchain {toolchain} is not in toolchains.toml"))
    return rows[toolchain]
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
def _omit(groups: list[tuple[str, ...]], omitted: Sequence[str]) -> list[tuple[str, ...]]:
    for text in omitted:
        if not any(text in (g[0], " ".join(g)) for g in groups):
            raise Refusal(Finding("recipe.option", f"cannot omit {text}: not in the compiler flags"))
        groups = [g for g in groups if text not in (g[0], " ".join(g))]
    return groups
def _make(toolchain: str, config: Config, cflags: Sequence[str]) -> Recipe:
    build = config.project.build
    cppflags = (*build["cppflags"], f"-D__UNBAKE_STDARG_{_row(toolchain)['family'].upper()}")
    asflags = tuple(build["asflags"]) + tuple(build["gnu_asflags"])
    flags = tuple(cflags)
    return Recipe(toolchain, cppflags, flags, asflags, digest((toolchain, cppflags, flags, asflags)))
def resolve(config: Config, unit: UnitSpec, overrides: Json) -> Recipe:
    for key in overrides:
        if key not in _KNOWN:
            raise Refusal(Finding("recipe.option", f"unknown override {key}", unit=unit.path))
    toolchain = overrides.get("toolchain") or unit.toolchain
    row = _row(toolchain)
    paired = tuple(row["paired"])
    supported = set(_words(row["supported_options"]))
    adds = list(overrides.get("add", ()))
    omits = list(overrides.get("omit", ()))
    for text in adds:
        if text in omits:
            raise Refusal(Finding("recipe.conflict", f"{text} is both added and omitted", unit=unit.path))
    base = [*row["cflags"], *config.project.build["cflags"], *unit.options.get("add", ())]
    groups = _groups(_words(base), paired)
    groups = _omit(groups, unit.options.get("omit", ()))
    groups = _omit(groups, omits)
    present = {word for g in groups for word in g}
    for text in adds:
        words = text.split()
        if not words or (words[0] not in supported and words[0] not in present):
            raise Refusal(Finding("recipe.option", f"cannot add {text}: not supported by {toolchain}", unit=unit.path))
        groups.extend(_groups(words, paired))
    return _make(toolchain, config, [word for g in groups for word in g])
def proposals(config: Config, recipe: Recipe) -> list[Recipe]:
    row = _row(recipe.toolchain)
    paired = tuple(row["paired"])
    supported = _words(row["supported_options"])
    flags = list(recipe.cflags)
    groups = _groups(flags, paired)
    seen = {recipe.digest}
    out: list[Recipe] = []
    def offer(candidate: Sequence[str]) -> None:
        proposal = _make(recipe.toolchain, config, candidate)
        if proposal.digest not in seen:
            seen.add(proposal.digest)
            out.append(proposal)
    for token in supported:
        if token not in flags:
            offer([*flags, token])
    for index, group in enumerate(groups):
        if group[0] in supported:
            offer([w for other in groups[:index] + groups[index + 1 :] for w in other])
    for index, group in enumerate(groups):
        if group[0] in _LEVELS:
            for level in _LEVELS:
                if level != group[0]:
                    offer([w for i, g in enumerate(groups) for w in ((level,) if i == index else g)])
    return out
