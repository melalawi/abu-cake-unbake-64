"""Render standalone builds with explicit compiler choices per source unit."""

from __future__ import annotations

import contextlib
import hashlib
import json
import shlex
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, overload

from unbake.project.cache import key
from unbake.project.config import Held, Policy, Project, SetupPolicy
from unbake.project_tools.compile_identity import driver_content, driver_names, driver_stamp_name

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
    from unbake.project.config import _read

    path = project.root / "config.toml"
    try:
        table = _read(path)["build"]
    except KeyError as error:
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
    if path.is_absolute() and not any(path.is_relative_to(root) for root in project.overlay_roots):
        raise Held("config", "build path must be inside the project or an explicit overlay root")
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


def host_executable(policy: Policy | SetupPolicy, value: str, field: str) -> str:
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
    result: list[str] = []
    for include in project.include:
        flag = "-I" + relative(project, include)
        if flag not in result:
            result.append(flag)
    result.extend(flag for flag in project.compiler_for(unit).cflags if flag not in result)
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
    return {
        "compilers": compilers,
        "default_compiler": project.default_compiler,
        "units": dict(project.units),
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


def helper_sources(project: Project) -> dict[str, str]:
    """Render implementation helpers without changing build recipe provenance."""
    tools = relative(project, project.tools)
    names = [
        "atomic.py",
        "extract.py",
        "compile.py",
        "compile_identity.py",
        "codegen.py",
        "elf.py",
        "layout.py",
        "link_inputs.py",
        "rodata.py",
        "literal_layout.py",
        "pool_slices.py",
        "host.py",
    ]
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
    return files


def helpers(project: Project) -> dict[str, str]:
    tools = relative(project, project.tools)
    files = helper_sources(project)
    data = description(project)
    files[tools + "/build.json"] = json.dumps(data, sort_keys=True, indent=2) + "\n"
    files.update(scoped_settings(project, data))
    return files


def scoped_settings(project: Project, data: dict[str, Any]) -> dict[str, str]:
    """Render all scoped recipe prerequisites from a verified build description."""
    tools = relative(project, project.tools)
    files = {}
    files[tools + "/link.json"] = json.dumps({"resident_mappings": data["resident_mappings"]}, sort_keys=True) + "\n"
    assembly = data["assembly_compiler"]
    files[tools + "/extract.json"] = (
        json.dumps(
            {
                "assembly_compiler": assembly,
                "compilers": {assembly: {"kind": data["compilers"][assembly]["kind"]}} if assembly else {},
            },
            sort_keys=True,
        )
        + "\n"
    )
    files.update(compile_settings(project, data=data))
    return files


def compile_settings(project: Project, *, data: dict[str, Any] | None = None) -> dict[str, str]:
    """Publish content-stable prerequisites for each compiler, version and unit flags."""
    data = description(project) if data is None else data
    tools = relative(project, project.tools)
    files = {}
    for version, macros in data["macros"].items():
        for ident, compiler in data["compilers"].items():
            settings = dict(
                compiler={key: value for key, value in compiler.items() if key != "as" or compiler["kind"] == "sn64"},
                include=data["include"],
                macros=macros,
            )
            if compiler["kind"] == "sn64":
                settings.update(cpp=data["cpp"], cppflags=data["cppflags"], asflags=data["sn64_asflags"])
            files[f"{tools}/compile/{version}/{ident}.json"] = json.dumps(settings, sort_keys=True) + "\n"
        sn64 = data["assembly_compiler"] is not None and data["compilers"][data["assembly_compiler"]]["kind"] == "sn64"
        settings = dict(asflags=data["asflags"], compiler=data["assembly_compiler"], assembler=data["as"])
        if sn64:
            compiler = data["compilers"][data["assembly_compiler"]]
            settings.update(assembler=compiler["as"], cpp=data["cpp"])
        files[f"{tools}/compile/{version}/assembly.json"] = json.dumps(settings, sort_keys=True) + "\n"
    units = {path.relative_to(project.src).with_suffix("").as_posix() for path in project.src.rglob("*.c")}
    units.update(data["units"])
    units.update(name.removeprefix(data["src"] + "/").removesuffix(".c") for name in data["unit_cflags"])
    for unit in sorted(units):
        direct = data["unit_cflags"].get(data["src"] + "/" + unit + ".c")
        flags = direct if direct is not None else data["unit_cflags"].get(Path(unit).stem, [])
        settings = dict(compiler=data["units"].get(Path(unit).stem, data["default_compiler"]), flags=flags)
        files[f"{tools}/compile/units/{unit}.json"] = json.dumps(settings, sort_keys=True) + "\n"
    files.update(driver_settings(project))
    return files


def driver_settings(project: Project) -> dict[str, str]:
    """Render scoped generator identities independently of compiler settings."""
    tools = relative(project, project.tools)
    files = {}
    if any(compiler.kind == "sn64" for compiler in project.compilers.values()):
        import abumasn64

        assert abumasn64.__file__ is not None
        files[f"{tools}/compile/drivers/abumasn64.sha256"] = (
            key(*(driver_content(path) for path in sorted(Path(abumasn64.__file__).parent.glob("*.py")))) + "\n"
        )
    for name in {name for kind in ("cc", "as") for sn64 in (False, True) for name in driver_names(kind, sn64)}:
        files[f"{tools}/compile/drivers/{name}.sha256"] = key(driver_content(TEMPLATES / name)) + "\n"
    for kind in ("cc", "as"):
        for sn64 in (False, True):
            name = driver_stamp_name("codegen.py", kind, sn64)
            files[f"{tools}/compile/drivers/{name}"] = key(driver_content(TEMPLATES / "codegen.py", kind, sn64)) + "\n"
    return files


def compile_rules(project: Project, *, data: dict[str, Any] | None = None) -> str:
    data = description(project) if data is None else data
    tools = relative(project, project.tools)

    def dependencies(ident: str | None, kind: str) -> str:
        compiler = data["compilers"][ident] if ident else None
        sn64 = compiler is not None and compiler["kind"] == "sn64"
        inputs = [f"{tools}/compile/drivers/{driver_stamp_name(name, kind, sn64)}" for name in driver_names(kind, sn64)]
        inputs.append(f"{tools}/compile/$(VERSION)/{ident if kind == 'cc' else 'assembly'}.json")
        if kind == "cc" and compiler:
            inputs.append(f"{tools}/compile/binaries/{ident}.sha256")
        if sn64 and compiler:
            inputs.extend(
                (
                    f"{tools}/compile/binaries/{hashlib.sha256(compiler['as'].encode()).hexdigest()}.sha256",
                    f"{tools}/compile/drivers/abumasn64.sha256",
                    f"{tools}/compile/binaries/{hashlib.sha256(data['cpp'].encode()).hexdigest()}.sha256",
                )
            )
        elif kind == "as":
            inputs.append(f"{tools}/compile/binaries/{hashlib.sha256(data['as'].encode()).hexdigest()}.sha256")
        return " ".join(inputs)

    overrides = " ".join("$(BUILD)/obj/src/" + unit + ".built" for unit in data["units"])
    lines = [f"$(filter-out {overrides},$(C_OBJECTS:.o=.built)): {dependencies(data['default_compiler'], 'cc')}"]
    for unit, ident in data["units"].items():
        lines.append(f"$(BUILD)/obj/src/{unit}.built: {dependencies(ident, 'cc')}")
    units = {path.relative_to(project.src).with_suffix("").as_posix() for path in project.src.rglob("*.c")}
    units.update(data["units"])
    units.update(name.removeprefix(data["src"] + "/").removesuffix(".c") for name in data["unit_cflags"])
    for unit in sorted(units):
        lines.append(f"$(BUILD)/obj/src/{unit}.built: {tools}/compile/units/{unit}.json")
    lines.append(f"$(ASM_OBJECTS:.o=.built): {dependencies(data['assembly_compiler'], 'as')}")
    assembly = data["assembly_compiler"]
    if assembly and data["compilers"][assembly]["kind"] == "sn64":
        lines.append("$(ASM_OBJECTS:.o=.built): $(BUILD)/obj/asm/%.built: $(BUILD)/asm-symbols/%.txt")
    return "\n".join(lines)


def render(project: Project, *, data: dict[str, Any] | None = None) -> dict[str, str]:
    build = recipe(project)
    project.version(project.names_from)
    values = {
        "TITLE": project.title,
        "NAMES_FROM": project.names_from,
        "ROM_INPUTS": "\n".join("- `" + relative(project, project.version(v).baserom) + "`" for v in project.versions),
        "NAME": project.name,
        "VERSIONS": " ".join(project.versions),
        "BUILD_ROOT": relative(project, project.build),
        "TOOLS": relative(project, project.tools),
        "SRC": relative(project, project.src),
        "ASM": relative(project, project.asm),
        "PINS": relative(project, project.tools / "compiler.sha256"),
        "LD": shell_words([host_tool(project, build.ld, "mips_ld")]),
        "OBJCOPY": shell_words([host_tool(project, build.objcopy, "mips_objcopy")]),
        "SPLAT": shell_words([host_tool(project, build.splat, "splat")]),
        "COMPILE_RULES": compile_rules(project, data=data),
    }
    blocks = []
    for name in project.versions:
        version = project.version(name)
        blocks.append(
            f"ifeq ($(VERSION),{name})\nSPLIT := {relative(project, version.split)}\n"
            f"SYMBOLS := {relative(project, version.symbols)}\n"
            f"BASEROM := {relative(project, version.baserom)}\nendif"
        )
    values["VERSION_BLOCKS"] = "\n".join(blocks)

    def fill(filename: str) -> str:
        content = (TEMPLATES / filename).read_text()
        for placeholder, value in values.items():
            content = content.replace("@" + placeholder + "@", value)
        return content

    files = helpers(project) if data is None else helper_sources(project) | scoped_settings(project, data)
    files.update({"Makefile": fill("Makefile"), "CONTRIBUTING.md": fill("CONTRIBUTING.md")})
    return files
