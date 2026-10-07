"""Render measured setup facts and explicitly confirmed ready configuration."""

from __future__ import annotations

import re
import tomllib
from typing import Any

import toml  # type: ignore[import-untyped]

from unbake.compilers import files as compiler_files
from unbake.config import Held, PendingProject
from unbake.process import capture
from unbake.process import named as cause_named
from unbake.project import header, rom
from unbake.project.census import Census


def version_macros(versions: tuple[str, ...]) -> dict[str, str]:
    """Give generated version branches distinct, valid C identifiers."""
    macros = {version: "VERSION_" + re.sub(r"[^A-Za-z0-9_]", "_", version).upper() for version in versions}
    if len(set(macros.values())) != len(macros):
        raise Held(
            cause_named(
                "project.versions",
                "project.versions: VERSION macro collision; supply distinct --version-name labels",
                owner="project.setup_config",
                stage="setup",
            )
        )
    return macros


def version_metadata(measured: header.Header, authored: dict[str, Any]) -> dict[str, str]:
    """Measured report labels for a new version; explicit authored labels remain authoritative."""
    fields = {
        "cartridge_id": measured.category + measured.game_code + measured.region,
        "region": header.DESTINATIONS[measured.region],
        "description": f"{measured.title}, revision {measured.revision}, CIC {measured.cic}.",
    }
    return {key: authored.get(key, value) for key, value in fields.items()}


def facts(project: PendingProject, census: Census, *, name: str | None, title: str | None) -> dict[str, Any]:
    with (project.root / "config.toml").open("rb") as source:
        data = tomllib.load(source)
    if name is None:
        name = data["project"].get("name", project.root.name)
    try:
        name = rom.stem(name, "project.name")
    except Held as error:
        raise Held(
            capture(
                error,
                cause=cause_named(
                    "project.name",
                    "project.name: supply --name STEM with a valid file stem",
                    owner="project.setup_config",
                    stage="setup",
                ),
            )
        ) from error
    reference = next(item for item in census.cartridges if census.names[item.path] == census.names_from)
    if title is None:
        title = data["project"].get("title", reference.header.title)
    if not isinstance(title, str) or not title.strip():
        raise Held(
            cause_named(
                "project.title", "project.title: supply --title TITLE", owner="project.setup_config", stage="setup"
            )
        )
    macros = version_macros(census.versions)
    data["project"].update(name=name, title=title, names_from=census.names_from, versions=list(census.versions))
    data["version"] = {
        census.names[item.path]: {
            **version_metadata(item.header, data.get("version", {}).get(census.names[item.path], {})),
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
        raise Held(
            cause_named(
                "project.state",
                "project.state: ready facts require the proved publication boundary",
                owner="project.setup_config",
                stage="setup",
            )
        )
    compiler_files.atomic_bytes(
        project.root / "config.toml", toml.dumps(facts(project, census, name=name, title=title)).encode()
    )


def exception_units(default_compiler: str, assignments: dict[str, str]) -> dict[str, dict[str, str]]:
    """A unit absent from [units] uses the default; list only the others, as { compiler = ID } rows."""
    return {name: {"compiler": ident} for name, ident in sorted(assignments.items()) if ident != default_compiler}


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
        raise Held(
            cause_named(
                "project.default_compiler",
                "project.default_compiler: explicit confirmed compiler required",
                owner="project.setup_config",
                stage="setup",
            )
        )
    if not assignments or set(assignments.values()) - cflags.keys():
        raise Held(
            cause_named(
                "units",
                "units: complete confirmed compiler assignments required",
                owner="project.setup_config",
                stage="setup",
            )
        )
    data = facts(project, census, name=name, title=title)
    data["project"].update(state="ready", default_compiler=default_compiler)
    data["compilers"] = {ident: {"cflags": list(flags)} for ident, flags in cflags.items()}
    units = exception_units(default_compiler, assignments)
    if units:
        data["units"] = units
    data["build"] = build
    return str(toml.dumps(data))


def canonical(text: str) -> str:
    """Retain current configuration, including explicit per-unit flags, without measurement state."""
    from unbake.config import CONFIG_SECTIONS

    return str(toml.dumps({key: value for key, value in tomllib.loads(text).items() if key in CONFIG_SECTIONS}))
