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

from unbake.compilers.config import BUILD_KEYS
from unbake.compilers.recipe_options import UnitRecipe, canonical_unit


def relative_text(root: Path, text: str) -> str:
    return re.sub(r"(?<![\w./%+)}-])/(?:[^\s\"']+)", lambda match: os.path.relpath(match[0], root), text)


def _held(kind: type[Held], args: tuple[Any, ...], state: dict[str, Any]) -> Held:
    held = kind.__new__(kind, *args)
    held.args = args
    held.__dict__.update(state)
    return held


def _cause(key: str, reason: str, *, owner: str, stage: str, action: Any = None) -> Any:
    from unbake.process import named as cause_named

    return cause_named(key, reason, owner=owner, stage=stage, action=action)


class Held(Exception):
    """One explicit owning Fault. Text and outer wrappers never infer its key."""

    def __init__(
        self, value: Any, *, failures: tuple[dict[str, Any], ...] = (), data: dict[str, Any] | None = None
    ) -> None:
        from unbake.process import Cause, Fault

        if not isinstance(value, (Cause, Fault)):
            raise TypeError("Held requires an explicit Cause or Fault")
        self.fault = Fault(value) if isinstance(value, Cause) else value
        self.failures = failures
        self.data = dict(data or {})
        if failures:
            self.data["failures"] = list(failures)
            self.fault = self.fault.framed(
                self.fault.cause.owner,
                self.fault.cause.stage,
                "per-version native proof failures",
                {"failures": list(failures)},
            )
        super().__init__(self.fault.cause.reason)

    @property
    def phase(self) -> str:
        return self.fault.cause.stage

    @property
    def key(self) -> str:
        return self.fault.cause.key

    @property
    def reason(self) -> str:
        return self.fault.cause.reason

    def __reduce__(self) -> tuple[Any, ...]:
        return _held, (type(self), self.args, self.__dict__)


class Unfinished(Held, NotImplementedError):
    """Named refusal for an interface whose implementation has not landed."""

    def __init__(self, phase: str, key: str) -> None:
        super().__init__(_cause(key, f"{key}: implementation required", owner="config", stage=phase))


# ---------------------------------------------------------------------------
# Project facts: config.toml with a fixed directory layout.

SCHEMA_VERSION = 2
CONFIG_SECTIONS = frozenset({"schema", "project", "compilers", "units", "version", "build"})
RETIRED_SECTIONS = ("paths", "workspace")


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
    def cache(self) -> Path:
        return self.root / ".unbake" / "cache"

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
    units: dict[str, UnitRecipe]
    version_map: dict[str, Version]
    id: str
    layout_cap: int
    asflags: tuple[str, ...]
    cppflags: tuple[str, ...]
    gnu_asflags: tuple[str, ...]
    resident_mappings: dict[str, tuple[ResidentMapping, ...]] = field(default_factory=dict)
    work_include: tuple[Path, ...] = ()
    source_bindings: tuple[tuple[str, str], ...] = ()
    admitted_roots: tuple[Path, ...] = ()

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
    def cache(self) -> Path:
        return Layout(self.root).cache

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

    def unit_path(self, unit: str | Path) -> str:
        path = Path(unit)
        if path.is_absolute() and path.is_relative_to(self.src):
            return canonical_unit(path.relative_to(self.root).as_posix())
        if str(path).startswith("src/"):
            return canonical_unit(path.as_posix())
        admitted = dict(self.source_bindings).get(str(path.resolve()))
        if admitted is not None:
            return canonical_unit(admitted)
        if path.is_absolute() and path.is_relative_to(self.work):
            from unbake.work.compare import function_of

            path = Path(function_of(path))
        if path.parent != Path(".") or path.suffix not in ("", ".c"):
            raise Held(
                _cause(
                    "recipe.unit",
                    f"{unit}: source path requires explicit logical TU admission",
                    owner="config",
                    stage="config",
                )
            )
        name = path.stem
        if not re.fullmatch(r"[A-Za-z_]\w*", name):
            raise Held(_cause("recipe.unit", f"{unit}: invalid logical TU", owner="config", stage="config"))
        from unbake.layout import split

        placements = {
            f"src/{row.path}.c"
            for version in self.versions
            for row in split.functions(self, version)
            if name in row.aliases
        }
        if len(placements) > 1:
            raise Held(
                _cause(
                    "recipe.unit",
                    f"{name}: conflicting TU placements {sorted(placements)}",
                    owner="config",
                    stage="config",
                )
            )
        return canonical_unit(next(iter(placements), f"src/{name}.c"))

    def recipe_for(self, unit: str | Path) -> UnitRecipe:
        return self.units.get(self.unit_path(unit), UnitRecipe(self.default_compiler))

    def compiler_reference(self, unit: str | Path) -> str:
        return self.recipe_for(unit).compiler

    def compiler_for(self, unit: str | Path) -> Compiler:
        ident = self.compiler_reference(unit)
        if ident not in self.compilers:
            raise Held(
                _cause(
                    f"[units].{Path(unit).stem}",
                    f"[units].{Path(unit).stem}: unknown compiler {ident}",
                    owner="config",
                    stage="config",
                )
            )
        return self.compilers[ident]

    def version(self, v: str) -> Version:
        if v not in self.version_map:
            raise Held(
                _cause(
                    "config.version",
                    f"{self.root / 'config.toml'} [version].{v}: unknown VERSION",
                    owner="config",
                    stage="config",
                )
            )
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
            raise Held(
                _cause(
                    f"{label}",
                    f"{label}: missing value; set it in config.toml before setup",
                    owner="config",
                    stage="config",
                )
            )
        return _strings(values[key], label)

    @property
    def asflags(self) -> tuple[str, ...]:
        return self.flags("asflags")

    @property
    def cppflags(self) -> tuple[str, ...]:
        return self.flags("cppflags")

    @property
    def gnu_asflags(self) -> tuple[str, ...]:
        return self.flags("gnu_asflags")


def _read(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as source:
            return tomllib.load(source)
    except (OSError, tomllib.TOMLDecodeError) as error:
        from unbake.process import capture

        raise Held(
            capture(error, cause=_cause(f"{path}", f"{path}: {error}", owner="config", stage="config"))
        ) from error


def _label(path: Path, table: str, name: str) -> str:
    return f"{path} [{table}].{name}" if table else f"{path} {name}"


def _required(values: dict[str, Any], name: str, label: str) -> Any:
    if name not in values:
        raise Held(_cause(f"{label}", f"{label}: missing value", owner="config", stage="config"))
    return values[name]


def _table(values: dict[str, Any], name: str, label: str) -> dict[str, Any]:
    value = _required(values, name, label)
    if not isinstance(value, dict):
        raise Held(_cause(f"{label}", f"{label}: expected table", owner="config", stage="config"))
    return value


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise Held(_cause(f"{label}", f"{label}: expected nonempty string", owner="config", stage="config"))
    return value


def _name(value: object, label: str) -> str:
    value = _text(value, label)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", value):
        raise Held(_cause(f"{label}", f"{label}: expected a single file stem", owner="config", stage="config"))
    return value


def _strings(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise Held(_cause(f"{label}", f"{label}: expected array of strings", owner="config", stage="config"))
    return tuple(_text(item, f"{label}[{index}]") for index, item in enumerate(value))


def _digest(value: object, length: int, label: str) -> str:
    value = _text(value, label)
    if not re.fullmatch(rf"[0-9a-fA-F]{{{length}}}", value):
        raise Held(
            _cause(f"{label}", f"{label}: expected {length}-digit hexadecimal digest", owner="config", stage="config")
        )
    return value.lower()


@overload
def _positive(value: object, label: str, *, integer: Literal[True]) -> int: ...


@overload
def _positive(value: object, label: str, *, integer: Literal[False]) -> float: ...


def _positive(value: object, label: str, *, integer: bool) -> int | float:
    valid = type(value) is int if integer else type(value) in (int, float)
    if not valid or not isinstance(value, (int, float)) or value <= 0 or not math.isfinite(value):
        kind = "integer" if integer else "number"
        raise Held(_cause(f"{label}", f"{label}: expected positive finite {kind}", owner="config", stage="config"))
    return int(value) if integer else float(value)


def _relative(value: object, label: str, root: Path) -> Path:
    spelling = _text(value, label)
    path = Path(spelling)
    if path.is_absolute() or ".." in path.parts or path == Path(".") or any(ord(char) < 32 for char in spelling):
        raise Held(_cause(f"{label}", f"{label}: expected project-relative path", owner="config", stage="config"))
    target = root / path
    if not target.resolve().is_relative_to(root):
        raise Held(_cause(f"{label}", f"{label}: symlink escapes project", owner="config", stage="config"))
    return target


def _refuse_retired(path: Path, data: dict[str, Any]) -> None:
    for section in RETIRED_SECTIONS:
        if section in data:
            raise Held(
                _cause(
                    "config._refuse_retired",
                    f"{path} [{section}]: retired section; remove it",
                    owner="config",
                    stage="config",
                )
            )


def discover(start: Path | None = None) -> Path:
    directory = (start or Path.cwd()).expanduser().resolve()
    for root in (directory, *directory.parents):
        if (root / "config.toml").is_file():
            return root
    raise Held(
        _cause(
            "project.root",
            "project.root: no project found from the working directory; supply --project DIR",
            owner="config",
            stage="config",
        )
    )


def load_pending(root: Path) -> PendingProject:
    root = Path(root).expanduser().resolve()
    return _pending(root, _read(root / "config.toml"))


def _pending(root: Path, data: dict[str, Any]) -> PendingProject:
    """Validate the same parsed document used by every ready/staged caller."""
    path = root / "config.toml"
    _refuse_retired(path, data)
    schema = _required(data, "schema", _label(path, "", "schema"))
    if type(schema) is not int or schema != SCHEMA_VERSION:
        raise Held(
            _cause("config.load_pending", f"{path} schema: expected {SCHEMA_VERSION}", owner="config", stage="config")
        )
    allowed = {
        "project": {"id", "state", "name", "title", "versions", "names_from", "default_compiler", "layout_cap"},
        "version": {"baserom", "baserom_sha1", "split", "symbols", "macros", "cartridge_id", "region", "description"},
        "compilers": {"cflags", "supply"},
    }
    for section in set(data) - CONFIG_SECTIONS:
        raise Held(_cause("config.unknown", f"{path} [{section}]: unknown section", owner="config", stage="config"))
    for section, keys in allowed.items():
        table = data.get(section, {})
        rows = [table] if section == "project" else list(table.values()) if isinstance(table, dict) else [table]
        for row in rows:
            if not isinstance(row, dict) or set(row) - keys:
                raise Held(
                    _cause(
                        "config.unknown",
                        f"{path} [{section}]: unknown fields or invalid table",
                        owner="config",
                        stage="config",
                    )
                )
    project = _table(data, "project", f"{path} [project]")
    state = _required(project, "state", _label(path, "project", "state"))
    if state not in ("awaiting-roms", "ready"):
        raise Held(
            _cause(
                f"{_label(path, 'project', 'state')}",
                f"{_label(path, 'project', 'state')}: expected awaiting-roms or ready",
                owner="config",
                stage="config",
            )
        )
    ident = _text(_required(project, "id", _label(path, "project", "id")), _label(path, "project", "id"))
    cap = _positive(
        _required(project, "layout_cap", _label(path, "project", "layout_cap")),
        _label(path, "project", "layout_cap"),
        integer=True,
    )
    build = data.get("build", {})
    if not isinstance(build, dict):
        raise Held(_cause("config.load_pending", f"{path} [build]: expected table", owner="config", stage="config"))
    unknown = sorted(set(build) - BUILD_KEYS)
    if unknown:
        raise Held(
            _cause("config.load_pending", f"{path} [build].{unknown[0]}: unknown key", owner="config", stage="config")
        )
    return PendingProject(root, ident, state, cap, tuple(sorted(build.items())))


def _resident(path: Path, table: object) -> dict[str, tuple[ResidentMapping, ...]]:
    label = f"{path} [build.resident_mappings]"
    if not isinstance(table, dict):
        raise Held(_cause(f"{label}", f"{label}: expected table", owner="config", stage="config"))
    result = {}
    for version, rows in table.items():
        if not isinstance(rows, list):
            raise Held(
                _cause(
                    f"{label}.{version}", f"{label}.{version}: expected array of tables", owner="config", stage="config"
                )
            )
        mappings = []
        for index, row in enumerate(rows):
            where = f"{label}.{version}[{index}]"
            if not isinstance(row, dict) or set(row) != {"address", "start", "end", "table_entry_bias"}:
                raise Held(
                    _cause(
                        f"{where}",
                        f"{where}: expected address, start, end, table_entry_bias",
                        owner="config",
                        stage="config",
                    )
                )
            if not all(type(row[key]) is int and row[key] >= 0 for key in row):
                raise Held(
                    _cause(f"{where}", f"{where}: expected non-negative integers", owner="config", stage="config")
                )
            mappings.append(ResidentMapping(row["address"], row["start"], row["end"], row["table_entry_bias"]))
        result[version] = tuple(mappings)
    return result


def load(root: Path, *, text: str | None = None) -> Project:
    """Load a ready project; text supplies staged config.toml content for this root."""
    root = Path(root).expanduser().resolve()
    path = root / "config.toml"
    if text is None:
        data = _read(path)
    else:
        try:
            data = tomllib.loads(text)
        except tomllib.TOMLDecodeError as error:
            from unbake.process import capture

            raise Held(
                capture(error, cause=_cause(f"{path}", f"{path}: {error}", owner="config", stage="config"))
            ) from error
    pending = _pending(root, data)
    if pending.state != "ready":
        raise Held(
            _cause("project.state", "project.state: awaiting-roms; run unbake setup", owner="config", stage="config")
        )
    _refuse_retired(path, data)
    unknown = sorted(set(data) - CONFIG_SECTIONS)
    if unknown:
        raise Held(_cause("config.load", f"{path} [{unknown[0]}]: unknown section", owner="config", stage="config"))
    project = _table(data, "project", f"{path} [project]")
    compiler_tables = _table(data, "compilers", f"{path} [compilers]")
    units_table = data.get("units", {})
    if not isinstance(units_table, dict):
        raise Held(_cause("config.load", f"{path} [units]: expected table", owner="config", stage="config"))
    version_tables = _table(data, "version", f"{path} [version]")
    build = _table(data, "build", f"{path} [build]")
    unknown = sorted(set(build) - BUILD_KEYS)
    if unknown:
        raise Held(_cause("config.load", f"{path} [build].{unknown[0]}: unknown key", owner="config", stage="config"))

    def value(table: dict[str, Any], section: str, name: str) -> Any:
        return _required(table, name, _label(path, section, name))

    name = _name(value(project, "project", "name"), _label(path, "project", "name"))
    title = _text(value(project, "project", "title"), _label(path, "project", "title"))
    versions_label = _label(path, "project", "versions")
    versions = _strings(value(project, "project", "versions"), versions_label)
    if not versions or len(set(versions)) != len(versions):
        raise Held(
            _cause(
                f"{versions_label}",
                f"{versions_label}: expected distinct nonempty VERSIONs",
                owner="config",
                stage="config",
            )
        )
    for v in versions:
        _name(v, versions_label)
        if v == "work":
            raise Held(
                _cause(
                    f"{versions_label}",
                    f"{versions_label}: VERSION 'work' collides with build/work",
                    owner="config",
                    stage="config",
                )
            )
    names_from = _text(value(project, "project", "names_from"), _label(path, "project", "names_from"))
    if names_from not in versions:
        raise Held(
            _cause(
                f"{_label(path, 'project', 'names_from')}",
                f"{_label(path, 'project', 'names_from')}: unknown VERSION {names_from}",
                owner="config",
                stage="config",
            )
        )
    from unbake.compilers.registry import compiler_directory, specification

    tools = Layout(root).tools
    if not compiler_tables:
        raise Held(
            _cause("config.load", f"{path} [compilers]: expected nonempty table", owner="config", stage="config")
        )
    compilers = {}
    for ident, table in compiler_tables.items():
        label = f"{path} [compilers.{ident}]"
        if not isinstance(table, dict):
            raise Held(_cause(f"{label}", f"{label}: expected table", owner="config", stage="config"))
        try:
            spec = specification(ident)
        except Held as error:
            from unbake.process import capture

            raise Held(
                capture(error, cause=_cause(f"{label}", f"{label}: {error.reason}", owner="config", stage="config"))
            ) from error
        cflags = _strings(_required(table, "cflags", label + ".cflags"), label + ".cflags")
        compilers[ident] = Compiler(
            ident,
            spec.kind,
            compiler_directory(tools, spec) / spec.cc,
            Path(spec.as_) if spec.as_.startswith("policy:") else compiler_directory(tools, spec) / spec.as_,
            cflags,
            tools / "compilers.sha256",
        )
    default_compiler = _text(value(project, "project", "default_compiler"), _label(path, "project", "default_compiler"))
    if default_compiler not in compilers:
        raise Held(
            _cause(
                "config.load",
                f"{path} [project].default_compiler: unknown compiler {default_compiler}",
                owner="config",
                stage="config",
            )
        )
    units = {}
    for unit, row in units_table.items():
        try:
            canonical_unit(unit)
            recipe = UnitRecipe.read(row)
            if recipe.compiler not in compilers:
                raise ValueError(f"unknown compiler {recipe.compiler}")
        except ValueError as error:
            raise Held(
                _cause("recipe.config", f"{path} [units].{unit}: {error}", owner="config", stage="config")
            ) from error
        units[unit] = recipe
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
            raise Held(
                _cause(
                    f"{_label(path, section, 'baserom')}",
                    f"{_label(path, section, 'baserom')}: expected a path under roms/",
                    owner="config",
                    stage="config",
                )
            )

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
        flags("gnu_asflags"),
        _resident(path, build["resident_mappings"]) if "resident_mappings" in build else {},
    )


# ---------------------------------------------------------------------------
# Host configuration: unbake.toml.

Kind = Literal["domain", "int", "path", "exe", "dirs", "fraction", "hex64", "text"]

HOST_KEYS: dict[str, dict[str, Kind]] = {
    "resources": {
        "domain": "domain",
        "cores": "int",
        "workers": "int",
        "memory_total_bytes": "int",
        "memory_parent_bytes": "int",
        "memory_worker_bytes": "int",
    },
    "cache": {"machine_root": "path", "max_bytes": "int", "trim_to_bytes": "int", "memory_bytes": "int"},
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
    "search": {"frontier": "int", "episode_recipes": "int", "episode_parents": "int", "no_gain_probes": "int"},
    # What a step chain may cost here (a project's .unbake/unbake.toml sets its own); over budget is a finding.
    "budgets": {
        "recompute_seconds": "int",
        "unchanged_seconds": "int",
        "changed_seconds": "int",
        "recompute_cpu_seconds": "int",
        "unchanged_cpu_seconds": "int",
        "changed_cpu_seconds": "int",
        "step_cpu_seconds": "int",
        "main_cpu_seconds": "int",
        "main_cpu_fraction": "fraction",
        "contended_cores": "int",
        "facts_miss_fraction": "fraction",
        "main_rss_bytes": "int",
        "worker_rss_bytes": "int",
    },
    "cycle": {
        "min_bytes": "int",
        "max_bytes": "int",
        "min_history": "int",
        "debounce_ms": "int",
        "search_seconds": "int",
    },
    "publish": {
        "branch": "text",
        "author_name": "text",
        "author_email": "text",
    },
}

_RESOURCES = (
    "resources.domain",
    "resources.cores",
    *(f"resources.memory_{n}_bytes" for n in ("total", "parent", "worker")),
)
_CACHE = ("cache.machine_root", "cache.max_bytes", "cache.trim_to_bytes", "cache.memory_bytes")
_BINUTILS = ("tools.cpp", "tools.mips_as", "tools.mips_ld", "tools.mips_objcopy", "tools.n64link")
# buildfiles writes the CI workflow for [publish].branch, so every command that may regenerate it needs it.
_BUILDFILES = (*_BINUTILS, "publish.branch")
# Every command that runs steps checks them against the budgets.
_BUDGETS = tuple(f"budgets.{name}" for name in HOST_KEYS["budgets"])
_COMPARE = (*_RESOURCES, *_CACHE, *_BUILDFILES, *_BUDGETS)
_PUBLISH = ("publish.branch", "publish.author_name", "publish.author_email")
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

# The host keys each command needs.
NEEDS: dict[str, tuple[str, ...]] = {
    "init": (),
    "migrate-state": (),
    "setup": _SETUP,
    "next": (*_RESOURCES, *_CACHE),
    "draft": (*_COMPARE, "tools.m2c", "tools.splat", "tools.mips_objdump"),
    "compare": _COMPARE,
    "tidy": (*_RESOURCES, *_CACHE, "tools.cpp"),
    "search-variants": (
        *_COMPARE,
        "tools.permuter_archive",
        "tools.permuter_sha256",
    ),
    "publish": (*_COMPARE, *_PUBLISH),
    "boundary": (*_RESOURCES, *_CACHE, "tools.splat", "tools.cpp"),
    "check": (*_RESOURCES, *_CACHE, *_BUILDFILES, "tools.make", "tools.path", *_BUDGETS),
    "explain": (*_RESOURCES, *_CACHE, "tools.cpp", "tools.mips_objdump", "tools.splat"),
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
        "cycle.search_seconds",
    ),
    "recompute": (*_RESOURCES, *_CACHE, *_BUILDFILES, *_BUDGETS),
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
            raise Held(
                _cause(
                    "config._host_table",
                    f"unbake.toml [{section}]: unknown section ({path})",
                    owner="config",
                    stage="config",
                )
            )
        if not isinstance(table, dict):
            raise Held(
                _cause(
                    "config._host_table",
                    f"unbake.toml [{section}]: expected table ({path})",
                    owner="config",
                    stage="config",
                )
            )
        for key in table:
            if key not in HOST_KEYS[section]:
                raise Held(
                    _cause(
                        "config._host_table",
                        f"unbake.toml [{section}].{key}: unknown key ({path})",
                        owner="config",
                        stage="config",
                    )
                )
        result[section] = dict(table)
    return result


def load_host(explicit: Path | None, project_root: Path | None, command: str) -> Host:
    """Read the user file, then let <project>/.unbake/unbake.toml replace single keys."""
    path = host_path(explicit)
    if not path.is_file():
        raise Held(_cause("unbake.toml", f"unbake.toml: missing file {path}", owner="config", stage="config"))
    values = _host_table(path, _read(path))
    sources = [path]
    if project_root is not None:
        override = Path(project_root) / ".unbake" / "unbake.toml"
        if override.is_file():
            for section, table in _host_table(override, _read(override)).items():
                if section == "resources" and "domain" in table:
                    raise Held(
                        _cause(
                            "config.load_host",
                            f"{override} [resources].domain: machine domain belongs to the host file",
                            owner="config",
                            stage="config",
                        )
                    )
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
            self.get(dotted)
        self._cross_checks(set(keys))

    def require_command(self, command: str) -> None:
        if command not in NEEDS:
            raise Held(
                _cause("config.require_command", f"command {command}: unknown command", owner="config", stage="config")
            )
        self.require(NEEDS[command])

    def _cross_checks(self, keys: set[str]) -> None:
        if {"cache.max_bytes", "cache.trim_to_bytes"} <= keys and self.cache_trim_to_bytes >= self.cache_max_bytes:
            raise Held(
                _cause(
                    "config._cross_checks",
                    "unbake.toml [cache].trim_to_bytes: expected less than [cache].max_bytes",
                    owner="config",
                    stage="config",
                )
            )
        if {"resources.memory_total_bytes", "resources.memory_parent_bytes", "resources.memory_worker_bytes"} <= keys:
            if self.memory_parent_bytes >= self.memory_total_bytes:
                raise Held(
                    _cause(
                        "config._cross_checks",
                        "unbake.toml [resources].memory_parent_bytes: "
                        "expected less than [resources].memory_total_bytes",
                        owner="config",
                        stage="config",
                    )
                )
            if self.memory_worker_bytes > self.memory_total_bytes - self.memory_parent_bytes:
                raise Held(
                    _cause(
                        "config._cross_checks",
                        "unbake.toml [resources].memory_worker_bytes: "
                        "expected at most memory_total_bytes - memory_parent_bytes",
                        owner="config",
                        stage="config",
                    )
                )

    def raw(self, dotted: str) -> Any:
        section, key = dotted.split(".", 1)
        if key not in HOST_KEYS.get(section, {}):
            raise Held(
                _cause(f"{self._label(dotted)}", f"{self._label(dotted)}: unknown key", owner="config", stage="config")
            )
        table = self.values.get(section, {})
        if key not in table:
            choices = '; set "standalone" or an absolute broker manifest path' if dotted == "resources.domain" else ""
            raise Held(
                _cause(
                    f"{self._label(dotted)}",
                    f"{self._label(dotted)}: missing value (needed by {self.command}){choices}",
                    owner="config",
                    stage="config",
                )
            )
        return table[key]

    def has(self, dotted: str) -> bool:
        section, key = dotted.split(".", 1)
        return key in self.values.get(section, {})

    def get(self, dotted: str) -> Any:
        section, key = dotted.split(".", 1)
        kind = HOST_KEYS[section][key]
        label = self._label(dotted)
        if dotted == "resources.workers":
            # Unset follows the host policy's core count. An explicit single worker serialises every pool job.
            if not self.has(dotted):
                return self.get("resources.cores")
            workers = _positive(self.raw(dotted), label, integer=True)
            if workers == 1 and self.get("resources.cores") > 1:
                raise Held(
                    _cause(
                        f"{label}",
                        f"{label}: 1 serialises every pool job on {self.get('resources.cores')} cores; "
                        "remove the key to use the core count or set more workers",
                        owner="config",
                        stage="config",
                    )
                )
            return workers
        value = self.raw(dotted)
        if kind == "domain":
            if value == "standalone":
                return value
            if not isinstance(value, str) or not value.strip() or not Path(value).expanduser().is_absolute():
                raise Held(
                    _cause(
                        f"{label}",
                        f'{label}: expected "standalone" or an absolute broker manifest path',
                        owner="config",
                        stage="config",
                    )
                )
            return Path(value).expanduser()
        if kind == "int":
            return _positive(value, label, integer=True)
        if kind == "fraction":
            if type(value) not in (int, float) or not 0 < value <= 1:
                raise Held(_cause(f"{label}", f"{label}: expected fraction in (0, 1]", owner="config", stage="config"))
            return float(value)
        if kind == "hex64":
            return _digest(value, 64, label)
        if kind == "text":
            return _text(value, label)
        if kind == "dirs":
            if not isinstance(value, list) or not value:
                raise Held(
                    _cause(
                        f"{label}", f"{label}: expected list of absolute directories", owner="config", stage="config"
                    )
                )
            directories = tuple(Path(_text(item, label)) for item in value)
            for directory in directories:
                if not directory.is_absolute() or not directory.is_dir():
                    raise Held(
                        _cause(
                            f"{label}",
                            f"{label}: expected list of absolute directories; {directory}",
                            owner="config",
                            stage="config",
                        )
                    )
            return directories
        path = Path(_text(value, label)).expanduser()
        if not path.is_absolute():
            raise Held(_cause(f"{label}", f"{label}: expected absolute path", owner="config", stage="config"))
        if kind == "exe" and (not path.is_file() or not os.access(path, os.X_OK)):
            raise Held(_cause(f"{label}", f"{label}: missing executable {path}", owner="config", stage="config"))
        return path

    # Typed accessors, one per key.
    domain = property(lambda self: self.get("resources.domain"))
    cores = property(lambda self: self.get("resources.cores"))
    workers = property(lambda self: self.get("resources.workers"))
    memory_total_bytes = property(lambda self: self.get("resources.memory_total_bytes"))
    memory_parent_bytes = property(lambda self: self.get("resources.memory_parent_bytes"))
    memory_worker_bytes = property(lambda self: self.get("resources.memory_worker_bytes"))
    cache_machine_root = property(lambda self: self.get("cache.machine_root"))
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
    search_frontier = property(lambda self: self.get("search.frontier") if self.has("search.frontier") else 8)
    no_gain_probes = property(
        lambda self: self.get("search.no_gain_probes") if self.has("search.no_gain_probes") else 32
    )
    episode_recipes = property(
        lambda self: self.get("search.episode_recipes") if self.has("search.episode_recipes") else 8
    )
    episode_parents = property(
        lambda self: self.get("search.episode_parents") if self.has("search.episode_parents") else 4
    )
    cycle_min_bytes = property(lambda self: self.get("cycle.min_bytes"))
    cycle_max_bytes = property(lambda self: self.get("cycle.max_bytes"))
    cycle_min_history = property(lambda self: self.get("cycle.min_history"))
    cycle_debounce_ms = property(lambda self: self.get("cycle.debounce_ms"))
    cycle_search_seconds = property(lambda self: self.get("cycle.search_seconds"))
    publish_branch = property(lambda self: self.get("publish.branch"))
    publish_author_name = property(lambda self: self.get("publish.author_name"))
    publish_author_email = property(lambda self: self.get("publish.author_email"))
    recompute_cpu_seconds = property(lambda self: self.get("budgets.recompute_cpu_seconds"))
    unchanged_cpu_seconds = property(lambda self: self.get("budgets.unchanged_cpu_seconds"))
    changed_cpu_seconds = property(lambda self: self.get("budgets.changed_cpu_seconds"))
    step_cpu_seconds = property(lambda self: self.get("budgets.step_cpu_seconds"))
    main_cpu_seconds = property(lambda self: self.get("budgets.main_cpu_seconds"))
    main_cpu_fraction = property(lambda self: self.get("budgets.main_cpu_fraction"))
    contended_cores = property(lambda self: self.get("budgets.contended_cores"))
    recompute_seconds = property(lambda self: self.get("budgets.recompute_seconds"))
    unchanged_seconds = property(lambda self: self.get("budgets.unchanged_seconds"))
    changed_seconds = property(lambda self: self.get("budgets.changed_seconds"))
    facts_miss_fraction = property(lambda self: self.get("budgets.facts_miss_fraction"))
    main_rss_bytes = property(lambda self: self.get("budgets.main_rss_bytes"))
    worker_rss_bytes = property(lambda self: self.get("budgets.worker_rss_bytes"))


@dataclass(frozen=True)
class SymbolPolicy:
    similarity_threshold: float
    similarity_margin: float


def draft_view(project: Project, function: str) -> Project:
    """The project as one function's draft sees it: its own build/work/FUNC/include/ before include/."""
    from dataclasses import replace

    return replace(project, work_include=(project.work / function / "include",))
