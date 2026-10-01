"""Render measured setup facts and explicitly confirmed ready configuration."""

from __future__ import annotations

import tomllib
from typing import Any

import toml  # type: ignore[import-untyped]

from unbake.project import compiler_files, rom
from unbake.project.census import Census
from unbake.project.config import Held, PendingProject


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
    data["project"].update(name=name, title=title, names_from=census.names_from, versions=list(census.versions))
    data["version"] = {
        census.names[item.path]: {
            "baserom": (project.roms / f"baserom.{census.names[item.path]}.z64").relative_to(project.root).as_posix(),
            "baserom_sha1": item.sha1,
            "split": f"versions/{census.names[item.path]}/{name}.yaml",
            "symbols": f"versions/{census.names[item.path]}/symbol_addrs.txt",
            "macros": [],
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
    data["units"] = assignments
    data["build"] = build
    return str(toml.dumps(data))
