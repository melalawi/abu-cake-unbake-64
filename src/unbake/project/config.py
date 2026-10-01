"""Required project facts and host process policy."""

import math
import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, overload

POLICY_PATH = Path(__file__).with_name("policy.toml")


class Held(Exception):
    def __init__(self, phase: str, reason: str) -> None:
        self.phase = phase
        self.reason = reason
        super().__init__(reason)


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


@dataclass(frozen=True)
class Project:
    root: Path
    name: str
    title: str
    names_from: str
    versions: tuple[str, ...]
    src: Path
    include: tuple[Path, ...]
    asm: Path
    tools: Path
    compilers: dict[str, Compiler]
    default_compiler: str
    units: dict[str, str]
    version_map: dict[str, Version]

    def compiler_for(self, unit: str | Path) -> Compiler:
        path = Path(unit)
        if path.is_absolute():
            try:
                path = path.relative_to(self.root)
            except ValueError:
                path = self.src.relative_to(self.root) / (path.stem + ".c")
        spelling = path.as_posix()
        direct = self.units.get(spelling)
        stem = self.units.get(path.stem)
        if direct and stem and direct != stem:
            raise Held("config", f"[units].{spelling}: conflicts with [units].{path.stem}")
        regions = set()
        if not direct and not stem and self.units:
            for version in self.version_map.values():
                segment = None
                try:
                    lines = version.split.read_text().splitlines()
                except OSError as error:
                    raise Held("config", f"{version.split}: {error}") from error
                for line in lines:
                    entry = re.match(r"^\s*-\s+name:\s*([^#]+?)\s*$", line)
                    if entry:
                        segment = entry[1].strip("\"'")
                    row = re.match(r"^\s*-\s*\[[^,]+,\s*(?:asm|c),\s*([^\]]+)\]", line)
                    if row and Path(row[1].strip().strip("\"'")).stem == path.stem and segment in self.units:
                        regions.add(self.units[segment])
            if len(regions) > 1:
                raise Held("config", f"[units].{path.stem}: conflicting segment compilers across VERSIONs")
        ident = direct or stem or next(iter(regions), self.default_compiler)
        if ident not in self.compilers:
            raise Held("config", f"[units].{spelling}: unknown compiler {ident}")
        return self.compilers[ident]

    def version(self, v: str) -> Version:
        if v not in self.version_map:
            raise Held("config", f"{self.root / 'config.toml'} [version].{v}: unknown VERSION")
        return self.version_map[v]

    def build_link(self, v: str) -> Path:
        self.version(v)
        return self.root / "build" / v


@dataclass(frozen=True)
class Policy:
    cores: int
    stall_trials: int
    search_beam: int
    assignment_idle_hours: float
    cache_root: Path
    state_root: Path
    objdiff_cli: Path
    objdiff_sha256: str
    m2c: Path
    splat: Path
    mips_ld: Path
    mips_objdump: Path
    mips_readelf: Path
    init_same_game_similarity: float
    init_split: str
    init_probe_count: int
    mips_as: Path
    mips_objcopy: Path
    cpp: Path
    asflags: tuple[str, ...]
    cppflags: tuple[str, ...]
    sn64_asflags: tuple[str, ...]
    permuter_archive: Path
    permuter_sha256: str


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


def _path(value: object, label: str, root: Path | None) -> Path:
    value = _text(value, label)
    try:
        path = Path(value).expanduser()
        if not path.is_absolute():
            if root is None:
                raise Held("config", f"{label}: expected absolute path or ~ path")
            path = root / path
        return path.absolute()
    except (OSError, RuntimeError, ValueError) as error:
        raise Held("config", f"{label}: {error}") from error


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


def load(root: Path) -> Project:
    root = Path(root).expanduser().resolve()
    path = root / "config.toml"
    data = _read(path)
    project = _table(data, "project", f"{path} [project]")
    paths = _table(data, "paths", f"{path} [paths]")
    compiler_tables = _table(data, "compilers", f"{path} [compilers]")
    units_table = _table(data, "units", f"{path} [units]")
    version_tables = _table(data, "version", f"{path} [version]")

    def value(table: dict[str, Any], section: str, field: str) -> Any:
        return _required(table, field, _label(path, section, field))

    name = _name(value(project, "project", "name"), _label(path, "project", "name"))
    title = _text(value(project, "project", "title"), _label(path, "project", "title"))
    versions_label = _label(path, "project", "versions")
    versions = _strings(value(project, "project", "versions"), versions_label)
    if not versions or len(set(versions)) != len(versions):
        raise Held("config", f"{versions_label}: expected distinct nonempty VERSIONs")
    for v in versions:
        _name(v, versions_label)
    names_from = (
        versions[0]
        if len(versions) == 1 and "names_from" not in project
        else _text(value(project, "project", "names_from"), _label(path, "project", "names_from"))
    )
    if names_from not in versions:
        raise Held("config", f"{_label(path, 'project', 'names_from')}: unknown VERSION {names_from}")

    def project_path(table: dict[str, Any], section: str, field: str) -> Path:
        return _path(value(table, section, field), _label(path, section, field), root)

    from unbake.project.toolchain import specification

    tools = project_path(paths, "paths", "tools")
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
        cflags = _strings(table["cflags"], label + ".cflags") if "cflags" in table else spec.cflags
        compilers[ident] = Compiler(
            ident,
            spec.kind,
            tools / ident / spec.cc,
            Path(spec.as_) if spec.as_.startswith("policy:") else tools / ident / spec.as_,
            cflags,
            tools / "compiler.sha256",
        )
    default_compiler = _text(value(project, "project", "default_compiler"), _label(path, "project", "default_compiler"))
    if default_compiler not in compilers:
        raise Held("config", f"{path} [project].default_compiler: unknown compiler {default_compiler}")
    units = {}
    for unit, ident in units_table.items():
        ident = _text(ident, f"{path} [units].{unit}")
        if ident not in compilers:
            raise Held("config", f"{path} [units].{unit}: unknown compiler {ident}")
        unit_path = Path(unit)
        if unit_path.is_absolute() or ".." in unit_path.parts:
            raise Held("config", f"{path} [units].{unit}: expected project-relative unit")
        units[unit] = ident
    version_map = {}
    for v in versions:
        section = f"version.{v}"
        table = _table(version_tables, v, f"{path} [{section}]")
        version_map[v] = Version(
            v,
            root / f"baserom.{v}.z64",
            _digest(value(table, section, "baserom_sha1"), 40, _label(path, section, "baserom_sha1")),
            project_path(table, section, "split"),
            project_path(table, section, "symbols"),
            _strings(value(table, section, "macros"), _label(path, section, "macros")),
        )
    includes = _strings(value(paths, "paths", "include"), _label(path, "paths", "include"))
    return Project(
        root,
        name,
        title,
        names_from,
        versions,
        project_path(paths, "paths", "src"),
        tuple(_path(item, _label(path, "paths", "include"), root) for item in includes),
        project_path(paths, "paths", "asm"),
        project_path(paths, "paths", "tools"),
        compilers,
        default_compiler,
        units,
        version_map,
    )


def load_policy(path: Path | None = None) -> Policy:
    if path is None:
        explicit = os.environ.get("UNBAKE_POLICY")
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
        path = Path(explicit) if explicit else base / "unbake" / "policy.toml"
    path = Path(path).expanduser().absolute()
    data = {**_read(POLICY_PATH), **_read(path)}

    def value(field: str) -> Any:
        return _required(data, field, _label(path, "", field))

    def host_path(field: str) -> Path:
        return _path(value(field), _label(path, "", field), None)

    similarity = value("init_same_game_similarity")
    if type(similarity) not in (int, float) or not 0 < similarity <= 1:
        raise Held("config", f"{path} init_same_game_similarity: expected number in (0, 1]")
    mode = value("init_split")
    if mode not in ("functions", "files"):
        raise Held("config", f"{path} init_split: expected functions or files")
    return Policy(
        _positive(value("cores"), _label(path, "", "cores"), integer=True),
        _positive(value("stall_trials"), _label(path, "", "stall_trials"), integer=True),
        _positive(value("search_beam"), _label(path, "", "search_beam"), integer=True),
        _positive(value("assignment_idle_hours"), _label(path, "", "assignment_idle_hours"), integer=False),
        host_path("cache_root"),
        host_path("state_root"),
        host_path("objdiff_cli"),
        _digest(value("objdiff_sha256"), 64, _label(path, "", "objdiff_sha256")),
        host_path("m2c"),
        host_path("splat"),
        host_path("mips_ld"),
        host_path("mips_objdump"),
        host_path("mips_readelf"),
        float(similarity),
        mode,
        _positive(value("init_probe_count"), _label(path, "", "init_probe_count"), integer=True),
        host_path("mips_as"),
        host_path("mips_objcopy"),
        host_path("cpp"),
        _strings(value("asflags"), _label(path, "", "asflags")),
        _strings(value("cppflags"), _label(path, "", "cppflags")),
        _strings(value("sn64_asflags"), _label(path, "", "sn64_asflags")),
        host_path("permuter_archive"),
        _digest(value("permuter_sha256"), 64, _label(path, "", "permuter_sha256")),
    )
