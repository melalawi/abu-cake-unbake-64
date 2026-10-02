"""Render measured setup facts and explicitly confirmed ready configuration."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Any

import toml  # type: ignore[import-untyped]

from unbake.project import compiler_files, rom
from unbake.project.census import Census
from unbake.project.config import Held, PendingProject


def version_macros(versions: tuple[str, ...]) -> dict[str, str]:
    """Give generated version branches distinct, valid C identifiers."""
    macros = {version: "VERSION_" + re.sub(r"[^A-Za-z0-9_]", "_", version).upper() for version in versions}
    if len(set(macros.values())) != len(macros):
        raise Held("setup", "project.versions: VERSION macro collision; supply distinct --version-name labels")
    return macros


def facts(project: PendingProject, census: Census, *, name: str | None, title: str | None) -> dict[str, Any]:
    with (project.root / "config.toml").open("rb") as source:
        data = tomllib.load(source)
    if name is None:
        name = data["project"].get("name", project.root.name)
    try:
        name = rom.stem(name, "project.name")
    except Held as error:
        raise Held("setup", "project.name: supply --name STEM with a valid file stem") from error
    reference = next(item for item in census.cartridges if census.names[item.path] == census.names_from)
    if title is None:
        title = data["project"].get("title", reference.header.title)
    if not isinstance(title, str) or not title.strip():
        raise Held("setup", "project.title: supply --title TITLE")
    macros = version_macros(census.versions)
    data["project"].update(name=name, title=title, names_from=census.names_from, versions=list(census.versions))
    data["version"] = {
        census.names[item.path]: {
            "baserom": (project.roms / f"baserom.{census.names[item.path]}.z64").relative_to(project.root).as_posix(),
            "baserom_sha1": item.sha1,
            "split": f"versions/{census.names[item.path]}/{name}.yaml",
            "symbols": f"versions/{census.names[item.path]}/symbol_addrs.txt",
            "macros": [macros[census.names[item.path]]],
        }
        for item in census.cartridges
    }
    return data


def write_facts(project: PendingProject, census: Census, *, name: str | None = None, title: str | None = None) -> None:
    """Persist accepted ROM/name facts while retaining awaiting-roms readiness."""
    if project.state != "awaiting-roms":
        raise Held("setup", "project.state: ready facts require the proved publication boundary")
    compiler_files.atomic_bytes(
        project.root / "config.toml", toml.dumps(facts(project, census, name=name, title=title)).encode()
    )


def exception_units(default_compiler: str, assignments: dict[str, str]) -> dict[str, str]:
    """A unit absent from [units] uses the default; list only the others."""
    return {name: ident for name, ident in sorted(assignments.items()) if ident != default_compiler}


def render_ready(
    project: PendingProject,
    census: Census,
    *,
    name: str,
    title: str,
    default_compiler: str,
    assignments: dict[str, str],
    cflags: dict[str, tuple[str, ...]],
    build: dict[str, Any],
) -> str:
    """Caller must confirm the proposal, then prove and atomically publish this text."""
    if not cflags or default_compiler not in cflags:
        raise Held("setup", "project.default_compiler: explicit confirmed compiler required")
    if not assignments or set(assignments.values()) - cflags.keys():
        raise Held("setup", "units: complete confirmed compiler assignments required")
    data = facts(project, census, name=name, title=title)
    data["project"].update(state="ready", default_compiler=default_compiler)
    data["compilers"] = {ident: {"cflags": list(flags)} for ident, flags in cflags.items()}
    units = exception_units(default_compiler, assignments)
    if units:
        data["units"] = units
    data["build"] = build
    return str(toml.dumps(data))


def canonical(text: str) -> str:
    """Rewrite a ready config.toml as configuration only.

    Measurements are not configuration: tool state and the build own them. A
    unit whose value is not a configured compiler, or equals the default, is
    not an exception. A default that is not a configured compiler becomes the
    compiler most units already use, the same rule the compiler proposal applies.
    """
    from collections import Counter

    from unbake.project.config import CONFIG_SECTIONS

    data = {key: value for key, value in tomllib.loads(text).items() if key in CONFIG_SECTIONS}
    compilers = data.get("compilers", {})
    concrete = {name: ident for name, ident in data.get("units", {}).items() if ident in compilers}
    default = data["project"].get("default_compiler")
    if default not in compilers:
        counts = Counter(concrete.values())
        if not counts:
            raise Held(
                "setup", "project.default_compiler: no configured compiler; run unbake setup --repropose-compilers"
            )
        default = min(counts, key=lambda ident: (-counts[ident], ident))
        data["project"]["default_compiler"] = default
    data.pop("units", None)
    units = exception_units(default, {Path(name).stem: ident for name, ident in concrete.items()})
    if units:
        data["units"] = units
    return str(toml.dumps(data))
