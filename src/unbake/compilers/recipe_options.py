"""Serializable phase recipes and ordered precedence, also usable by standalone builds.

This module is pure: no registry, filesystem, native process, or project state.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

PHASES = ("preprocess", "compile", "assemble", "link")
PAIRS = frozenset({"-I", "-D", "-U", "-include", "-imacros", "-isystem", "-iquote"})


@dataclass(frozen=True)
class OptionSpec:
    id: str
    phase: str
    tokens: tuple[str, ...]
    values: tuple[str | int, ...] = ()
    exclusive_group: str | None = None
    scope: str = "translation_unit"
    prerequisites: tuple[str, ...] = ()
    conflicts: tuple[str, ...] = ()
    semantic_risks: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()


def canonical_unit(value: str) -> str:
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or str(path) != value or path.suffix != ".c" or path.parts[0] != "src":
        raise ValueError(f"recipe.unit: {value}: expected canonical src/ translation unit path")
    return value


def phase_options(value: Mapping[str, Any]) -> tuple[tuple[str, tuple[str, ...]], ...]:
    if not isinstance(value, dict) or set(value) != set(PHASES):
        raise ValueError("recipe.options: all four explicit phases required; unknown phases refused")
    result = []
    for phase in PHASES:
        tokens = value[phase]
        if not isinstance(tokens, (list, tuple)) or any(not isinstance(t, str) or not t or "\n" in t for t in tokens):
            raise ValueError(f"recipe.options.{phase}: ordered tokens required")
        result.append((phase, tuple(tokens)))
    return tuple(result)


@dataclass(frozen=True)
class UnitRecipe:
    compiler: str
    options: tuple[tuple[str, tuple[str, ...]], ...] = tuple((p, ()) for p in PHASES)
    functions: tuple[tuple[str, tuple[tuple[str, tuple[str, ...]], ...]], ...] = ()

    def phase(self, name: str) -> tuple[str, ...]:
        return dict(self.options)[name]

    def document(self) -> dict[str, Any]:
        return {
            "compiler": self.compiler,
            "options": {p: list(v) for p, v in self.options},
            **(
                {"functions": {name: {"options": {p: list(v) for p, v in options}} for name, options in self.functions}}
                if self.functions
                else {}
            ),
        }

    @classmethod
    def read(cls, value: Any) -> UnitRecipe:
        if (
            not isinstance(value, dict)
            or set(value) - {"compiler", "options", "functions"}
            or not {"compiler", "options"} <= value.keys()
        ):
            raise ValueError("recipe: expected compiler, options and optional bound functions")
        if not isinstance(value["compiler"], str) or not value["compiler"]:
            raise ValueError("recipe.compiler: nonempty pinned compiler ID required")
        functions = value.get("functions", {})
        if not isinstance(functions, dict):
            raise ValueError("recipe.functions: binding table required")
        bound = []
        for binding, row in sorted(functions.items()):
            if not isinstance(row, dict) or set(row) != {"options"} or not binding:
                raise ValueError("recipe.functions: explicit placement binding and options required")
            bound.append((binding, phase_options(row["options"])))
        return cls(value["compiler"], phase_options(value["options"]), tuple(bound))


@dataclass(frozen=True)
class ResolvedRecipe:
    unit: str
    compiler: str
    phases: tuple[tuple[str, tuple[str, ...]], ...]
    pins: tuple[tuple[str, str], ...] = ()
    binding: str | None = None

    def phase(self, name: str) -> tuple[str, ...]:
        return dict(self.phases)[name]

    def document(self) -> dict[str, Any]:
        return {
            "unit": self.unit,
            "compiler": self.compiler,
            "options": {p: list(v) for p, v in self.phases},
            "pins": dict(self.pins),
            "binding": self.binding,
        }

    @property
    def digest(self) -> str:
        return recipe_digest(self.document())


def recipe_digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def partition(tokens: tuple[str, ...]) -> dict[str, list[str]]:
    """Migration/default argv split; explicit current records already name phases."""
    result: dict[str, list[str]] = {p: [] for p in PHASES}
    iterator = iter(tokens)
    for token in iterator:
        if token in PAIRS:
            argument = next(iterator, None)
            if not argument or argument.startswith("-"):
                raise ValueError(f"recipe.options: {token}: missing value")
            result["preprocess"].extend((token, argument))
        elif token.startswith(("-I", "-D", "-U")):
            result["preprocess"].append(token)
        elif token != "-c":
            result["compile"].append(token)
    return result


def _group(token: str) -> str | None:
    for pattern, group in (
        (r"-G\d+", "small-data"),
        (r"-O[0-3s]?", "optimization"),
        (r"-g[0-3]?", "debug"),
        (r"-mips[1-4]", "isa"),
        (r"-mgp(?:32|64)", "gp-width"),
        (r"-mfp(?:32|64)", "fp-width"),
        (r"-f(?:un)?signed-char", "char-sign"),
        (r"-m(?:soft|hard)-float", "float-abi"),
    ):
        if re.fullmatch(pattern, token):
            return group
    if token.startswith("-f"):
        return token.removeprefix("-f").removeprefix("no-")
    return None


def option_group(token: str, phase: str, specs: tuple[OptionSpec, ...]) -> str | None:
    for spec in specs:
        if spec.phase != phase:
            continue
        for spelling in spec.tokens:
            pattern = re.escape(spelling).replace(re.escape("{value}"), "(.+)")
            match = re.fullmatch(pattern, token)
            if match:
                if spec.values and (not match.groups() or match[1] not in {str(v) for v in spec.values}):
                    raise ValueError(f"recipe.options.{phase}: {token}: outside pinned option space")
                return spec.exclusive_group
    return _group(token)


def merge_options(
    *layers: tuple[str, ...], phase: str = "compile", specs: tuple[OptionSpec, ...] = ()
) -> tuple[str, ...]:
    ordered: list[tuple[str | None, tuple[str, ...]]] = []
    for layer in layers:
        iterator = iter(layer)
        for token in iterator:
            words: tuple[str, ...]
            if token in PAIRS:
                argument = next(iterator, None)
                if not argument or argument.startswith("-"):
                    raise ValueError(f"recipe.options: {token}: missing value")
                words, group = (token, argument), None
            else:
                words, group = (token,), option_group(token, phase, specs)
            if group is not None:
                ordered = [(g, v) for g, v in ordered if g != group]
            ordered.append((group, words))
    return tuple(t for _, words in ordered for t in words)


def resolve_options(
    unit: str,
    recipe: UnitRecipe,
    defaults: Mapping[str, Any],
    *,
    registry_defaults: Mapping[str, Any] | None = None,
    trial: Mapping[str, Any] | None = None,
    version_macros: tuple[str, ...] = (),
    pins: Mapping[str, str] | None = None,
    binding: str | None = None,
    separately_compiled: bool = False,
    specs: tuple[OptionSpec, ...] = (),
) -> ResolvedRecipe:
    canonical_unit(unit)
    layers = [
        dict(phase_options(dict(registry_defaults or {p: [] for p in PHASES}))),
        dict(phase_options(dict(defaults))),
        {p: tuple("-D" + m for m in version_macros) if p == "preprocess" else () for p in PHASES},
        dict(recipe.options),
    ]
    if binding in dict(recipe.functions):
        if not separately_compiled:
            raise ValueError("recipe.scope: function options require a proved separately compiled placement binding")
        layers.append(dict(dict(recipe.functions)[binding]))
    if trial is not None:
        layers.append(dict(phase_options(dict(trial))))
    phases = tuple((p, merge_options(*(tuple(layer[p]) for layer in layers), phase=p, specs=specs)) for p in PHASES)
    return ResolvedRecipe(unit, recipe.compiler, phases, tuple(sorted((pins or {}).items())), binding)
