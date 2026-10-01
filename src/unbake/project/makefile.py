"""Render standalone builds with explicit compiler choices per source unit."""

from __future__ import annotations

import contextlib
import json
import re
import shlex
import tomllib
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, overload

from unbake.project.config import Held, Policy, Project

TEMPLATES = Path(__file__).parents[1] / "project_tools"


@dataclass(frozen=True)
class Recipe:
    ld: str
    objcopy: str
    splat: str
    asflags: tuple[str, ...]
    sn64_asflags: tuple[str, ...]
    assembly_compiler: str | None
    as_: str | None
    cpp: str | None
    cppflags: tuple[str, ...]
    unit_cflags: dict[str, tuple[str, ...]]
    resident_mappings: dict[str, list[dict[str, int]]]


def recipe(project: Project) -> Recipe:
    path = project.root / "config.toml"
    try:
        table = tomllib.loads(path.read_text())["build"]
    except (OSError, ValueError, KeyError) as error:
        raise Held("config", f"{path} [build]: {error}") from error

    @overload
    def required(name: str, array: Literal[False] = False) -> str: ...

    @overload
    def required(name: str, array: Literal[True]) -> tuple[str, ...]: ...

    def required(name: str, array: bool = False) -> str | tuple[str, ...]:
        value = table.get(name)
        valid = (
            (isinstance(value, list) and all(isinstance(x, str) and x for x in value))
            if array
            else isinstance(value, str) and bool(value)
        )
        if not valid:
            raise Held("config", f"{path} [build].{name}: missing or invalid value")
        if array:
            return tuple(str(item) for item in value)
        return str(value)

    assembly = table.get("assembly_compiler")
    if assembly is not None and assembly not in project.compilers:
        raise Held("config", f"{path} [build].assembly_compiler: unknown compiler {assembly}")
    host_as = required("as") if assembly is None else None
    sn64 = any(c.kind == "sn64" for c in project.compilers.values())
    unit_cflags = table.get("unit_cflags", {})
    if not isinstance(unit_cflags, dict) or any(
        not isinstance(name, str)
        or not name
        or not isinstance(value, list)
        or any(not isinstance(flag, str) or not flag for flag in value)
        for name, value in unit_cflags.items()
    ):
        raise Held("config", f"{path} [build].unit_cflags: expected unit names and flag arrays")
    from unbake.project_tools.layout import resident_mappings

    mappings = table.get("resident_mappings", {})
    if not isinstance(mappings, dict) or any(v not in project.versions for v in mappings):
        raise Held("config", f"{path} [build].resident_mappings: expected VERSION tables")
    try:
        mappings = {v: resident_mappings(rows) for v, rows in mappings.items()}
    except ValueError as error:
        raise Held("config", f"{path} [build].{error}") from error
    return Recipe(
        required("ld"),
        required("objcopy"),
        required("splat"),
        required("asflags", True),
        required("sn64_asflags", True) if sn64 else (),
        assembly,
        host_as,
        required("cpp") if sn64 else None,
        required("cppflags", True) if sn64 else (),
        {name: tuple(value) for name, value in unit_cflags.items()},
        mappings,
    )


def relative(project: Project, path: Path) -> str:
    path = Path(path)
    if path.is_absolute():
        with contextlib.suppress(ValueError):
            path = path.relative_to(project.root)
    if path.is_absolute():
        raise Held("config", "build path must be inside the project")
    value = str(path)
    if not value or any(c.isspace() or c in "#$:%\\" for c in value):
        raise Held("config", f"build path {value!r} cannot be represented in Make")
    return value


def host_tool(project: Project, value: str, name: str) -> str:
    """Keep host locations in operator policy, and project tools relative."""
    if value.startswith("policy:"):
        return value
    path = Path(value)
    if path.is_absolute():
        if path.is_relative_to(project.root):
            return relative(project, path)
        return "policy:" + name
    return value


def host_executable(policy: Policy, value: str, field: str) -> str:
    """Resolve a recipe host tool; policy:NAME references read the operator policy, as the build does."""
    if not value:
        raise Held("config", f"build.{field}: missing value")
    if not value.startswith("policy:"):
        return value
    name = value.removeprefix("policy:")
    configured = getattr(policy, name, None)
    if configured is None or not str(configured):
        raise Held("config", f"policy.{name}: missing executable for build.{field}")
    return str(configured)


def flags(project: Project, version: str, unit: str | Path) -> tuple[str, ...]:
    result = list(project.compiler_for(unit).cflags)
    for include in project.include:
        flag = "-I" + relative(project, include)
        if flag not in result:
            result.append(flag)
    result.extend("-D" + macro for macro in project.version(version).macros)
    overrides = recipe(project).unit_cflags
    path = Path(unit)
    if path.is_absolute():
        path = (
            path.relative_to(project.root)
            if path.is_relative_to(project.root)
            else project.src.relative_to(project.root) / (path.stem + ".c")
        )
    direct, stem = overrides.get(str(path)), overrides.get(path.stem)
    if direct is not None and stem is not None and direct != stem:
        raise Held("config", f"[build].unit_cflags.{path}: conflicting stem flags")
    result.extend(direct if direct is not None else stem if stem is not None else ())
    return tuple(result)


def shell_words(words: Iterable[str | Path]) -> str:
    return " ".join(shlex.quote(str(word)).replace("$", "$$") for word in words)


def description(project: Project) -> dict[str, Any]:
    build = recipe(project)
    compilers = {}
    for ident, compiler in project.compilers.items():
        compilers[ident] = {
            "kind": compiler.kind,
            "cc": relative(project, compiler.cc),
            "as": str(compiler.as_) if str(compiler.as_).startswith("policy:") else relative(project, compiler.as_),
            "cflags": list(compiler.cflags),
        }
    units = dict(project.units)
    memberships: dict[str, set[str]] = {}
    for version in project.version_map.values():
        segment = None
        for line in version.split.read_text().splitlines():
            entry = re.match(r"^\s*-\s+name:\s*([^#]+?)\s*$", line)
            if entry:
                segment = entry[1].strip("\"'")
            row = re.match(r"^\s*-\s*\[[^,]+,\s*(?:asm|c),\s*([^\]]+)\]", line)
            if row and segment in project.units:
                name = Path(row[1].strip().strip("\"'")).stem
                memberships.setdefault(name, set()).add(project.units[segment])
    for name, choices in memberships.items():
        source = project.src / (name + ".c")
        if str(source.relative_to(project.root)) in project.units or name in project.units:
            units[name] = project.compiler_for(source).id
        elif len(choices) == 1:
            units[name] = next(iter(choices))
        else:
            raise Held("config", f"[units].{name}: conflicting segment compilers across VERSIONs")
    return {
        "compilers": compilers,
        "default_compiler": project.default_compiler,
        "units": units,
        "src": relative(project, project.src),
        "include": [relative(project, p) for p in project.include],
        "asm": relative(project, project.asm),
        "macros": {v: list(project.version(v).macros) for v in project.versions},
        "asflags": list(build.asflags),
        "sn64_asflags": list(build.sn64_asflags),
        "assembly_compiler": build.assembly_compiler,
        "as": host_tool(project, build.as_, "mips_as") if build.as_ else None,
        "cpp": host_tool(project, build.cpp, "cpp") if build.cpp else None,
        "cppflags": list(build.cppflags),
        "unit_cflags": build.unit_cflags,
        "resident_mappings": build.resident_mappings,
    }


def linker_script(script: str, rows: list[dict[str, Any]]) -> str:
    """Link proved shared compiler sections over their resident split rows."""
    from unbake.project_tools.rodata import fragment, insert_fragment

    try:
        return insert_fragment(script, fragment(rows))
    except ValueError as error:
        raise Held("build", str(error)) from error


def helpers(project: Project) -> dict[str, str]:
    tools = relative(project, project.tools)
    names = ["extract.py", "compile.py", "elf.py", "layout.py", "rodata.py", "literal_layout.py", "host.py"]
    if any(c.kind == "sn64" for c in project.compilers.values()):
        names.extend(
            [
                "sn64_cc.py",
                "resolve_external_branches.py",
            ]
        )
    files = {
        tools + "/" + name: (TEMPLATES / name)
        .read_text()
        .replace("from unbake.project_tools.", "from ")
        .replace("from unbake.project.cache import", "from cache import")
        for name in names
    }
    cache_source = Path(__file__).with_name("cache.py").read_text()
    cache_source = cache_source.replace(
        "from unbake.project.config import Held",
        """class Held(Exception):
    def __init__(self, phase, reason):
        super().__init__(f"HELD({phase}): {reason}")""",
    )
    files[tools + "/cache.py"] = cache_source
    files[tools + "/build.json"] = json.dumps(description(project), sort_keys=True, indent=2) + "\n"
    return files


def render(project: Project) -> dict[str, str]:
    build = recipe(project)
    project.version(project.names_from)
    values = {
        "TITLE": project.title,
        "NAME": project.name,
        "VERSIONS": " ".join(project.versions),
        "TOOLS": relative(project, project.tools),
        "SRC": relative(project, project.src),
        "ASM": relative(project, project.asm),
        "PINS": relative(project, project.tools / "compiler.sha256"),
        "LD": shell_words([host_tool(project, build.ld, "mips_ld")]),
        "OBJCOPY": shell_words([host_tool(project, build.objcopy, "mips_objcopy")]),
        "SPLAT": shell_words([host_tool(project, build.splat, "splat")]),
        "DRIVERS": " ".join(
            "$(TOOLS)/" + name
            for name in (
                ["compile.py", "cache.py", "elf.py", "host.py"]
                + (
                    [
                        "sn64_cc.py",
                        "resolve_external_branches.py",
                    ]
                    if any(c.kind == "sn64" for c in project.compilers.values())
                    else []
                )
            )
        ),
    }
    blocks = []
    for name in project.versions:
        version = project.version(name)
        blocks.append(
            f"ifeq ($(VERSION),{name})\nSPLIT := {relative(project, version.split)}\n"
            f"SYMBOLS := {relative(project, version.symbols)}\nendif"
        )
    values["VERSION_BLOCKS"] = "\n".join(blocks)

    def fill(filename: str) -> str:
        content = (TEMPLATES / filename).read_text()
        for key, value in values.items():
            content = content.replace("@" + key + "@", value)
        return content

    files = helpers(project)
    files.update({"Makefile": fill("Makefile"), "CONTRIBUTING.md": fill("CONTRIBUTING.md")})
    return files
