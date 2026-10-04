"""Project facts (config.toml) and host configuration (unbake.toml).

Both refuse a missing or invalid value by name. Nothing has a packaged default.
"""

from __future__ import annotations

import math
import os
import re
import tomllib
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, overload


def relative_text(root: Path, text: str) -> str:
    return re.sub(r"(?<![\w])/(?:[^\s\"']+)", lambda match: os.path.relpath(match[0], root), text)


class Held(Exception):
    def __init__(self, phase: str, reason: str, *, next_action: str | None = None) -> None:
        self.phase = phase
        self.reason = reason
        self.next_action = next_action
        super().__init__(reason)

    @property
    def key(self) -> str:
        """The stable name of the refusal: the reason text before its first colon."""
        return self.reason.split(":", 1)[0].strip()


class Unfinished(Held, NotImplementedError):
    """Named refusal for an interface whose implementation has not landed."""

    def __init__(self, phase: str, key: str) -> None:
        super().__init__(phase, f"{key}: implementation required")


# ---------------------------------------------------------------------------
# Project facts: config.toml with a fixed directory layout.

SCHEMA_VERSION = 1
CONFIG_SECTIONS = frozenset({"schema", "project", "compilers", "units", "version", "build"})
RETIRED_SECTIONS = ("paths", "workspace")
BUILD_KEYS = frozenset({"asflags", "cppflags", "sn64_asflags", "resident_mappings"})


@dataclass(frozen=True)
class Compiler:
    id: str
    kind: str
    cc: Path
    as_: Path
    cflags: tuple[str, ...]
    sha256: Path


@dataclass(frozen=True)
class Version:
    name: str
    baserom: Path
    baserom_sha1: str
    split: Path
    symbols: Path
    macros: tuple[str, ...]
    cartridge_id: str = ""
    region: str = ""
    description: str = ""


@dataclass(frozen=True)
class ResidentMapping:
    address: int
    start: int
    end: int
    table_entry_bias: int


@dataclass(frozen=True)
class Layout:
    """The fixed directory layout of every project."""

    root: Path

    @property
    def roms(self) -> Path:
        return self.root / "roms"

    @property
    def build(self) -> Path:
        return self.root / "build"

    @property
    def work(self) -> Path:
        return self.root / "build" / "work"

    @property
    def src(self) -> Path:
        return self.root / "src"

    @property
    def include(self) -> tuple[Path, ...]:
        return (self.root / "include",)

    @property
    def tools(self) -> Path:
        return self.root / "tools"



@dataclass(frozen=True)
class Project:
    root: Path
    name: str
    title: str
    names_from: str
    versions: tuple[str, ...]
    compilers: dict[str, Compiler]
    default_compiler: str
    units: dict[str, str]
    version_map: dict[str, Version]
    id: str
    layout_cap: int
    asflags: tuple[str, ...]
    cppflags: tuple[str, ...]
    sn64_asflags: tuple[str, ...]
    resident_mappings: dict[str, tuple[ResidentMapping, ...]] = field(default_factory=dict)
    unit_flags: dict[str, tuple[str, ...]] = field(default_factory=dict)
    work_include: tuple[Path, ...] = ()

    @property
    def roms(self) -> Path:
        return Layout(self.root).roms

    @property
    def build(self) -> Path:
        return Layout(self.root).build

    @property
    def work(self) -> Path:
        return Layout(self.root).work

    @property
    def src(self) -> Path:
        return Layout(self.root).src

    @property
    def include(self) -> tuple[Path, ...]:
        """A draft's own header directory comes first, then the project's include/."""
        return (*self.work_include, *Layout(self.root).include)

    @property
    def tools(self) -> Path:
        return Layout(self.root).tools

    def compiler_reference(self, unit: str | Path) -> str:
        """An exception unit names its compiler; every other unit uses the default."""
        return self.units.get(Path(unit).stem, self.default_compiler)

    def compiler_for(self, unit: str | Path) -> Compiler:
        ident = self.compiler_reference(unit)
        if ident not in self.compilers:
            raise Held("config", f"[units].{Path(unit).stem}: unknown compiler {ident}")
        return self.compilers[ident]

    def version(self, v: str) -> Version:
        if v not in self.version_map:
            raise Held("config", f"{self.root / 'config.toml'} [version].{v}: unknown VERSION")
        return self.version_map[v]

    def build_link(self, v: str) -> Path:
        self.version(v)
        return self.build / v


@dataclass(frozen=True)
class PendingProject(Layout):
    """A project created by init whose ROMs are not set up yet."""

    id: str = ""
    state: str = ""
    layout_cap: int = 0
    build_table: tuple[tuple[str, Any], ...] = ()

    def flags(self, key: str) -> tuple[str, ...]:
        """A [build] flag list that setup needs before it can probe compilers."""
        label = _label(self.root / "config.toml", "build", key)
        values = dict(self.build_table)
        if key not in values:
            raise Held("config", f"{label}: missing value; set it in config.toml before setup")
        return _strings(values[key], label)

    @property
    def asflags(self) -> tuple[str, ...]:
        return self.flags("asflags")

    @property
    def cppflags(self) -> tuple[str, ...]:
        return self.flags("cppflags")

    @property
    def sn64_asflags(self) -> tuple[str, ...]:
        return self.flags("sn64_asflags")


def _read(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as source:
            return tomllib.load(source)
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise Held("config", f"{path}: {error}") from error


def _label(path: Path, table: str, name: str) -> str:
    return f"{path} [{table}].{name}" if table else f"{path} {name}"


def _required(values: dict[str, Any], name: str, label: str) -> Any:
    if name not in values:
        raise Held("config", f"{label}: missing value")
    return values[name]


def _table(values: dict[str, Any], name: str, label: str) -> dict[str, Any]:
    value = _required(values, name, label)
    if not isinstance(value, dict):
        raise Held("config", f"{label}: expected table")
    return value


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise Held("config", f"{label}: expected nonempty string")
    return value


def _name(value: object, label: str) -> str:
    value = _text(value, label)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", value):
        raise Held("config", f"{label}: expected a single file stem")
    return value


def _strings(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise Held("config", f"{label}: expected array of strings")
    return tuple(_text(item, f"{label}[{index}]") for index, item in enumerate(value))


def _digest(value: object, length: int, label: str) -> str:
    value = _text(value, label)
    if not re.fullmatch(rf"[0-9a-fA-F]{{{length}}}", value):
        raise Held("config", f"{label}: expected {length}-digit hexadecimal digest")
    return value.lower()


@overload
def _positive(value: object, label: str, *, integer: Literal[True]) -> int: ...


@overload
def _positive(value: object, label: str, *, integer: Literal[False]) -> float: ...


def _positive(value: object, label: str, *, integer: bool) -> int | float:
    valid = type(value) is int if integer else type(value) in (int, float)
    if not valid or not isinstance(value, (int, float)) or value <= 0 or not math.isfinite(value):
        kind = "integer" if integer else "number"
        raise Held("config", f"{label}: expected positive finite {kind}")
    return int(value) if integer else float(value)


def _relative(value: object, label: str, root: Path) -> Path:
    spelling = _text(value, label)
    path = Path(spelling)
    if path.is_absolute() or ".." in path.parts or path == Path(".") or any(ord(char) < 32 for char in spelling):
        raise Held("config", f"{label}: expected project-relative path")
    target = root / path
    if not target.resolve().is_relative_to(root):
        raise Held("config", f"{label}: symlink escapes project")
    return target


def _refuse_retired(path: Path, data: dict[str, Any]) -> None:
    for section in RETIRED_SECTIONS:
        if section in data:
            raise Held("config", f"{path} [{section}]: retired section; remove it")


def discover(start: Path | None = None) -> Path:
    directory = (start or Path.cwd()).expanduser().resolve()
    for root in (directory, *directory.parents):
        if (root / "config.toml").is_file():
            return root
    raise Held("config", "project.root: no project found from the working directory; supply --project DIR")


def load_pending(root: Path) -> PendingProject:
    root = Path(root).expanduser().resolve()
    path = root / "config.toml"
    data = _read(path)
    _refuse_retired(path, data)
    schema = _required(data, "schema", _label(path, "", "schema"))
    if type(schema) is not int or schema != SCHEMA_VERSION:
        raise Held("config", f"{path} schema: expected {SCHEMA_VERSION}")
    project = _table(data, "project", f"{path} [project]")
    state = _required(project, "state", _label(path, "project", "state"))
    if state not in ("awaiting-roms", "ready"):
        raise Held("config", f"{_label(path, 'project', 'state')}: expected awaiting-roms or ready")
    ident = _text(_required(project, "id", _label(path, "project", "id")), _label(path, "project", "id"))
    cap = _positive(
        _required(project, "layout_cap", _label(path, "project", "layout_cap")),
        _label(path, "project", "layout_cap"),
        integer=True,
    )
    build = data.get("build", {})
    if not isinstance(build, dict):
        raise Held("config", f"{path} [build]: expected table")
    unknown = sorted(set(build) - BUILD_KEYS)
    if unknown:
        raise Held("config", f"{path} [build].{unknown[0]}: unknown key")
    return PendingProject(root, ident, state, cap, tuple(sorted(build.items())))


def _resident(path: Path, table: object) -> dict[str, tuple[ResidentMapping, ...]]:
    label = f"{path} [build.resident_mappings]"
    if not isinstance(table, dict):
        raise Held("config", f"{label}: expected table")
    result = {}
    for version, rows in table.items():
        if not isinstance(rows, list):
            raise Held("config", f"{label}.{version}: expected array of tables")
        mappings = []
        for index, row in enumerate(rows):
            where = f"{label}.{version}[{index}]"
            if not isinstance(row, dict) or set(row) != {"address", "start", "end", "table_entry_bias"}:
                raise Held("config", f"{where}: expected address, start, end, table_entry_bias")
            if not all(type(row[key]) is int and row[key] >= 0 for key in row):
                raise Held("config", f"{where}: expected non-negative integers")
            mappings.append(ResidentMapping(row["address"], row["start"], row["end"], row["table_entry_bias"]))
        result[version] = tuple(mappings)
    return result


def load(root: Path, *, text: str | None = None) -> Project:
    """Load a ready project; text supplies staged config.toml content for this root."""
    root = Path(root).expanduser().resolve()
    path = root / "config.toml"
    pending = load_pending(root)
    if pending.state != "ready":
        raise Held("config", "project.state: awaiting-roms; run unbake setup")
    if text is None:
        data = _read(path)
    else:
        try:
            data = tomllib.loads(text)
        except tomllib.TOMLDecodeError as error:
            raise Held("config", f"{path}: {error}") from error
    _refuse_retired(path, data)
    unknown = sorted(set(data) - CONFIG_SECTIONS)
    if unknown:
        raise Held("config", f"{path} [{unknown[0]}]: unknown section")
    project = _table(data, "project", f"{path} [project]")
    compiler_tables = _table(data, "compilers", f"{path} [compilers]")
    units_table = data.get("units", {})
    if not isinstance(units_table, dict):
        raise Held("config", f"{path} [units]: expected table")
    version_tables = _table(data, "version", f"{path} [version]")
    build = _table(data, "build", f"{path} [build]")
    unknown = sorted(set(build) - BUILD_KEYS)
    if unknown:
        raise Held("config", f"{path} [build].{unknown[0]}: unknown key")

    def value(table: dict[str, Any], section: str, name: str) -> Any:
        return _required(table, name, _label(path, section, name))

    name = _name(value(project, "project", "name"), _label(path, "project", "name"))
    title = _text(value(project, "project", "title"), _label(path, "project", "title"))
    versions_label = _label(path, "project", "versions")
    versions = _strings(value(project, "project", "versions"), versions_label)
    if not versions or len(set(versions)) != len(versions):
        raise Held("config", f"{versions_label}: expected distinct nonempty VERSIONs")
    for v in versions:
        _name(v, versions_label)
        if v == "work":
            raise Held("config", f"{versions_label}: VERSION 'work' collides with build/work")
    names_from = _text(value(project, "project", "names_from"), _label(path, "project", "names_from"))
    if names_from not in versions:
        raise Held("config", f"{_label(path, 'project', 'names_from')}: unknown VERSION {names_from}")
    from unbake.compilers.registry import specification

    tools = Layout(root).tools
    if not compiler_tables:
        raise Held("config", f"{path} [compilers]: expected nonempty table")
    compilers = {}
    for ident, table in compiler_tables.items():
        label = f"{path} [compilers.{ident}]"
        if not isinstance(table, dict):
            raise Held("config", f"{label}: expected table")
        try:
            spec = specification(ident)
        except Held as error:
            raise Held("config", f"{label}: {error.reason}") from error
        cflags = _strings(_required(table, "cflags", label + ".cflags"), label + ".cflags")
        compilers[ident] = Compiler(
            ident,
            spec.kind,
            tools / ident / spec.cc,
            Path(spec.as_) if spec.as_.startswith("policy:") else tools / ident / spec.as_,
            cflags,
            tools / "compilers.sha256",
        )
    default_compiler = _text(value(project, "project", "default_compiler"), _label(path, "project", "default_compiler"))
    if default_compiler not in compilers:
        raise Held("config", f"{path} [project].default_compiler: unknown compiler {default_compiler}")
    units = {}
    unit_flags = {}
    for unit, row in units_table.items():
        label = f"{path} [units].{unit}"
        if not re.fullmatch(r"[A-Za-z_]\w*", unit):
            raise Held("config", f"{label}: expected a function name")
        if not isinstance(row, dict) or set(row) - {"compiler", "flags"} or "compiler" not in row:
            raise Held("config", f"{label}: expected {{ compiler = ID, flags = [...] }}")
        ident = _text(row["compiler"], label + ".compiler")
        flags_row = _strings(row.get("flags", []), label + ".flags")
        if ident not in compilers:
            raise Held("config", f"{label}.compiler: unknown compiler {ident}")
        if ident == default_compiler and not flags_row:
            raise Held("config", f"{label}: equals the default compiler with no flags; remove the row")
        units[unit] = ident
        if flags_row:
            unit_flags[unit] = flags_row
    version_map = {}
    for v in versions:
        section = f"version.{v}"
        table = _table(version_tables, v, f"{path} [{section}]")

        def located(field_name: str, table: dict[str, Any] = table, section: str = section) -> Path:
            return _relative(value(table, section, field_name), _label(path, section, field_name), root)

        version_map[v] = Version(
            v,
            located("baserom"),
            _digest(value(table, section, "baserom_sha1"), 40, _label(path, section, "baserom_sha1")),
            located("split"),
            located("symbols"),
            _strings(value(table, section, "macros"), _label(path, section, "macros")),
            *(
                _text(table[key], _label(path, section, key)) if key in table else ""
                for key in ("cartridge_id", "region", "description")
            ),
        )
        if not version_map[v].baserom.is_relative_to(Layout(root).roms):
            raise Held("config", f"{_label(path, section, 'baserom')}: expected a path under roms/")

    def flags(key: str) -> tuple[str, ...]:
        return _strings(value(build, "build", key), _label(path, "build", key))

    return Project(
        root,
        name,
        title,
        names_from,
        versions,
        compilers,
        default_compiler,
        units,
        version_map,
        pending.id,
        pending.layout_cap,
        flags("asflags"),
        flags("cppflags"),
        flags("sn64_asflags"),
        _resident(path, build["resident_mappings"]) if "resident_mappings" in build else {},
        unit_flags,
    )


# ---------------------------------------------------------------------------
# Host configuration: unbake.toml.

Kind = Literal["int", "path", "exe", "dirs", "fraction", "hex64", "text", "envname"]

HOST_KEYS: dict[str, dict[str, Kind]] = {
    "resources": {
        "cores": "int",
        "workers": "int",
        "memory_total_bytes": "int",
        "memory_parent_bytes": "int",
        "memory_worker_bytes": "int",
    },
    "cache": {"root": "path", "max_bytes": "int", "trim_to_bytes": "int", "memory_bytes": "int"},
    "tools": {
        "make": "exe",
        "path": "dirs",
        "cpp": "exe",
        "mips_as": "exe",
        "mips_ld": "exe",
        "mips_objcopy": "exe",
        "mips_objdump": "exe",
        "mips_readelf": "exe",
        "n64link": "exe",
        "splat": "exe",
        "m2c": "exe",
        "permuter_archive": "path",
        "permuter_sha256": "hex64",
    },
    "setup": {
        "version_jobs": "int",
        "probe_count": "int",
        "same_game_similarity": "fraction",
        "symbol_similarity_threshold": "fraction",
        "symbol_similarity_margin": "fraction",
    },
    "search": {"stall_trials": "int", "beam": "int"},
    "cycle": {"min_bytes": "int", "max_bytes": "int", "min_history": "int", "debounce_ms": "int"},
    "publish": {
        "remote": "text",
        "branch": "text",
        "author_name": "text",
        "author_email": "text",
        "credential_env": "envname",
        "credential_helper": "text",
    },
}

_RESOURCES = ("resources.cores", "resources.workers", *(f"resources.memory_{n}_bytes" for n in ("total", "parent", "worker")))
_CACHE = ("cache.root", "cache.max_bytes", "cache.trim_to_bytes", "cache.memory_bytes")
_BINUTILS = ("tools.cpp", "tools.mips_as", "tools.mips_ld", "tools.mips_objcopy", "tools.n64link")
# buildfiles writes the CI workflow for [publish].branch, so every command that may regenerate it needs it.
_BUILDFILES = (*_BINUTILS, "publish.branch")
_COMPARE = (*_RESOURCES, *_CACHE, *_BUILDFILES)
_PUBLISH = ("publish.remote", "publish.branch", "publish.author_name", "publish.author_email", "publish.credential")
_SETUP = (
    *_RESOURCES,
    *_CACHE,
    *_BUILDFILES,
    "tools.make",
    "tools.path",
    "tools.splat",
    "setup.version_jobs",
    "setup.probe_count",
    "setup.same_game_similarity",
    "setup.symbol_similarity_threshold",
    "setup.symbol_similarity_margin",
)

# The host keys each command needs. "publish.credential" means exactly one of the two credential keys.
NEEDS: dict[str, tuple[str, ...]] = {
    "init": (),
    "setup": _SETUP,
    "next": _CACHE,
    "draft": (*_COMPARE, "tools.m2c", "tools.splat", "tools.mips_objdump"),
    "compare": _COMPARE,
    "tidy": (*_CACHE, "tools.cpp"),
    "search-variants": (
        *_COMPARE,
        "search.stall_trials",
        "search.beam",
        "tools.permuter_archive",
        "tools.permuter_sha256",
    ),
    "publish": (*_COMPARE, *_PUBLISH),
    "boundary": (*_RESOURCES, *_CACHE, "tools.splat", "tools.cpp"),
    "check": (*_RESOURCES, *_CACHE, *_BUILDFILES, "tools.make", "tools.path"),
    "explain": (*_CACHE, "tools.cpp", "tools.mips_objdump", "tools.splat"),
    "cycle": (
        *_COMPARE,
        *_PUBLISH,
        "tools.m2c",
        "tools.splat",
        "tools.mips_objdump",
        "cycle.min_bytes",
        "cycle.max_bytes",
        "cycle.min_history",
        "cycle.debounce_ms",
    ),
    "recompute": (*_RESOURCES, *_CACHE, *_BUILDFILES),
}


def host_path(explicit: Path | None) -> Path:
    """--config, then UNBAKE_CONFIG, then the XDG config directory."""
    if explicit is not None:
        return Path(explicit).expanduser().absolute()
    named = os.environ.get("UNBAKE_CONFIG")
    if named:
        return Path(named).expanduser().absolute()
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base).expanduser().absolute() / "unbake" / "unbake.toml"


def _host_table(path: Path, data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for section, table in data.items():
        if section not in HOST_KEYS:
            raise Held("config", f"unbake.toml [{section}]: unknown section ({path})")
        if not isinstance(table, dict):
            raise Held("config", f"unbake.toml [{section}]: expected table ({path})")
        for key in table:
            if key not in HOST_KEYS[section]:
                raise Held("config", f"unbake.toml [{section}].{key}: unknown key ({path})")
        result[section] = dict(table)
    return result


def load_host(explicit: Path | None, project_root: Path | None, command: str) -> Host:
    """Read the user file, then let <project>/.unbake/unbake.toml replace single keys."""
    path = host_path(explicit)
    if not path.is_file():
        raise Held("config", f"unbake.toml: missing file {path}")
    values = _host_table(path, _read(path))
    sources = [path]
    if project_root is not None:
        override = Path(project_root) / ".unbake" / "unbake.toml"
        if override.is_file():
            for section, table in _host_table(override, _read(override)).items():
                values.setdefault(section, {}).update(table)
            sources.append(override)
    host = Host(values, command, tuple(sources))
    host.require_command(command)
    return host


@dataclass(frozen=True)
class Host:
    """Validated access to unbake.toml values; every value is checked when first read."""

    values: dict[str, dict[str, Any]]
    command: str
    sources: tuple[Path, ...] = ()

    @classmethod
    def from_values(cls, values: dict[str, dict[str, Any]], command: str) -> Host:
        return cls(_host_table(Path("<values>"), values), command)

    def _label(self, dotted: str) -> str:
        section, key = dotted.split(".", 1)
        return f"unbake.toml [{section}].{key}"

    def require(self, keys: Iterable[str]) -> None:
        for dotted in keys:
            if dotted == "publish.credential":
                self.credential()
            else:
                self.get(dotted)
        self._cross_checks(set(keys))

    def require_command(self, command: str) -> None:
        if command not in NEEDS:
            raise Held("config", f"command {command}: unknown command")
        self.require(NEEDS[command])

    def _cross_checks(self, keys: set[str]) -> None:
        if {"cache.max_bytes", "cache.trim_to_bytes"} <= keys and self.cache_trim_to_bytes >= self.cache_max_bytes:
            raise Held("config", "unbake.toml [cache].trim_to_bytes: expected less than [cache].max_bytes")
        if {"resources.memory_total_bytes", "resources.memory_parent_bytes", "resources.memory_worker_bytes"} <= keys:
            if self.memory_parent_bytes >= self.memory_total_bytes:
                raise Held(
                    "config",
                    "unbake.toml [resources].memory_parent_bytes: expected less than [resources].memory_total_bytes",
                )
            if self.memory_worker_bytes > self.memory_total_bytes - self.memory_parent_bytes:
                raise Held(
                    "config",
                    "unbake.toml [resources].memory_worker_bytes: expected at most memory_total_bytes - memory_parent_bytes",
                )

    def raw(self, dotted: str) -> Any:
        section, key = dotted.split(".", 1)
        if key not in HOST_KEYS.get(section, {}):
            raise Held("config", f"{self._label(dotted)}: unknown key")
        table = self.values.get(section, {})
        if key not in table:
            raise Held("config", f"{self._label(dotted)}: missing value (needed by {self.command})")
        return table[key]

    def has(self, dotted: str) -> bool:
        section, key = dotted.split(".", 1)
        return key in self.values.get(section, {})

    def get(self, dotted: str) -> Any:
        section, key = dotted.split(".", 1)
        kind = HOST_KEYS[section][key]
        value = self.raw(dotted)
        label = self._label(dotted)
        if kind == "int":
            return _positive(value, label, integer=True)
        if kind == "fraction":
            if type(value) not in (int, float) or not 0 < value <= 1:
                raise Held("config", f"{label}: expected fraction in (0, 1]")
            return float(value)
        if kind == "hex64":
            return _digest(value, 64, label)
        if kind == "text":
            return _text(value, label)
        if kind == "envname":
            text = _text(value, label)
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", text):
                raise Held("config", f"{label}: expected environment variable name")
            return text
        if kind == "dirs":
            if not isinstance(value, list) or not value:
                raise Held("config", f"{label}: expected list of absolute directories")
            directories = tuple(Path(_text(item, label)) for item in value)
            for directory in directories:
                if not directory.is_absolute() or not directory.is_dir():
                    raise Held("config", f"{label}: expected list of absolute directories; {directory}")
            return directories
        path = Path(_text(value, label)).expanduser()
        if not path.is_absolute():
            raise Held("config", f"{label}: expected absolute path")
        if kind == "exe" and (not path.is_file() or not os.access(path, os.X_OK)):
            raise Held("config", f"{label}: missing executable {path}")
        return path

    def credential(self) -> tuple[str, str]:
        """("env", NAME) or ("helper", COMMAND): exactly one of the two keys."""
        present = [key for key in ("credential_env", "credential_helper") if self.has("publish." + key)]
        if len(present) != 1:
            raise Held("config", "unbake.toml [publish]: set exactly one of credential_env, credential_helper")
        key = present[0]
        return ("env" if key == "credential_env" else "helper"), self.get("publish." + key)

    # Typed accessors, one per key.
    cores = property(lambda self: self.get("resources.cores"))
    workers = property(lambda self: self.get("resources.workers"))
    memory_total_bytes = property(lambda self: self.get("resources.memory_total_bytes"))
    memory_parent_bytes = property(lambda self: self.get("resources.memory_parent_bytes"))
    memory_worker_bytes = property(lambda self: self.get("resources.memory_worker_bytes"))
    cache_root = property(lambda self: self.get("cache.root"))
    cache_max_bytes = property(lambda self: self.get("cache.max_bytes"))
    cache_trim_to_bytes = property(lambda self: self.get("cache.trim_to_bytes"))
    cache_memory_bytes = property(lambda self: self.get("cache.memory_bytes"))
    make = property(lambda self: self.get("tools.make"))
    tool_path = property(lambda self: self.get("tools.path"))
    cpp = property(lambda self: self.get("tools.cpp"))
    mips_as = property(lambda self: self.get("tools.mips_as"))
    mips_ld = property(lambda self: self.get("tools.mips_ld"))
    mips_objcopy = property(lambda self: self.get("tools.mips_objcopy"))
    mips_objdump = property(lambda self: self.get("tools.mips_objdump"))
    mips_readelf = property(lambda self: self.get("tools.mips_readelf"))
    n64link = property(lambda self: self.get("tools.n64link"))
    splat = property(lambda self: self.get("tools.splat"))
    m2c = property(lambda self: self.get("tools.m2c"))
    permuter_archive = property(lambda self: self.get("tools.permuter_archive"))
    permuter_sha256 = property(lambda self: self.get("tools.permuter_sha256"))
    setup_version_jobs = property(lambda self: self.get("setup.version_jobs"))
    probe_count = property(lambda self: self.get("setup.probe_count"))
    same_game_similarity = property(lambda self: self.get("setup.same_game_similarity"))
    symbol_similarity_threshold = property(lambda self: self.get("setup.symbol_similarity_threshold"))
    symbol_similarity_margin = property(lambda self: self.get("setup.symbol_similarity_margin"))
    stall_trials = property(lambda self: self.get("search.stall_trials"))
    search_beam = property(lambda self: self.get("search.beam"))
    cycle_min_bytes = property(lambda self: self.get("cycle.min_bytes"))
    cycle_max_bytes = property(lambda self: self.get("cycle.max_bytes"))
    cycle_min_history = property(lambda self: self.get("cycle.min_history"))
    cycle_debounce_ms = property(lambda self: self.get("cycle.debounce_ms"))
    publish_remote = property(lambda self: self.get("publish.remote"))
    publish_branch = property(lambda self: self.get("publish.branch"))
    publish_author_name = property(lambda self: self.get("publish.author_name"))
    publish_author_email = property(lambda self: self.get("publish.author_email"))


@dataclass(frozen=True)
class SymbolPolicy:
    similarity_threshold: float
    similarity_margin: float


def draft_view(project: Project, function: str) -> Project:
    """The project as one function's draft sees it: its own build/work/FUNC/include/ before include/."""
    from dataclasses import replace

    return replace(project, work_include=(project.work / function / "include",))
