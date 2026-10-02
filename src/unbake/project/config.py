"""Required project facts and host process policy."""

import math
import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, cast, overload

POLICY_PATH = Path(__file__).with_name("policy.toml")


class Held(Exception):
    def __init__(self, phase: str, reason: str) -> None:
        self.phase = phase
        self.reason = reason
        super().__init__(reason)


class Unfinished(Held, NotImplementedError):
    """Named refusal for an interface whose implementation has not landed."""

    def __init__(self, phase: str, key: str) -> None:
        super().__init__(phase, f"{key}: implementation required")


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
    id: str
    workspace_id: str
    roms: Path
    build: Path
    work: Path
    drafts: Path
    compiler_ties: dict[str, tuple[str, ...]] = field(default_factory=dict)

    def compiler_id(self, ident: str) -> str:
        """A carrier for draft/assembly recipes, never a regional pin."""
        return self.compiler_ties[ident][0] if ident in self.compiler_ties else ident

    def compiler_reference(self, unit: str | Path) -> str:
        """Return the configured ID or explicit candidate-set reference."""
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
        return ident

    def compiler_for(self, unit: str | Path) -> Compiler:
        ident = self.compiler_id(self.compiler_reference(unit))
        if ident not in self.compilers:
            raise Held("config", f"[units].{unit}: unknown compiler {ident}")
        return self.compilers[ident]

    def version(self, v: str) -> Version:
        if v not in self.version_map:
            raise Held("config", f"{self.root / 'config.toml'} [version].{v}: unknown VERSION")
        return self.version_map[v]

    def build_link(self, v: str) -> Path:
        self.version(v)
        return self.build / v


@dataclass(frozen=True)
class CensusPolicy:
    same_game_similarity: float


@dataclass(frozen=True)
class SymbolPolicy:
    similarity_threshold: float
    similarity_margin: float


@dataclass(frozen=True)
class SetupPolicy(CensusPolicy):
    cores: int
    cache_root: Path
    splat: Path
    mips_as: Path
    mips_ld: Path
    mips_objcopy: Path
    cpp: Path
    asflags: tuple[str, ...]
    cppflags: tuple[str, ...]
    sn64_asflags: tuple[str, ...]
    probe_count: int
    setup_version_jobs: int
    symbol_similarity_threshold: float
    symbol_similarity_margin: float


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
    same_game_similarity: float
    probe_count: int
    mips_as: Path
    mips_objcopy: Path
    cpp: Path
    asflags: tuple[str, ...]
    cppflags: tuple[str, ...]
    sn64_asflags: tuple[str, ...]
    permuter_archive: Path
    permuter_sha256: str
    setup_version_jobs: int


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


SCHEMA_VERSION = 1


@dataclass(frozen=True)
class PendingProject:
    root: Path
    id: str
    workspace_id: str
    state: str
    roms: Path
    build: Path
    work: Path
    drafts: Path
    src: Path
    include: tuple[Path, ...]
    asm: Path
    tools: Path


def discover(start: Path | None = None) -> Path:
    directory = (start or Path.cwd()).expanduser().resolve()
    for root in (directory, *directory.parents):
        if (root / "config.toml").is_file():
            return root
    raise Held("config", "project.root: no created project found from cwd; supply --project DIR")


def _relative(value: object, label: str, root: Path) -> Path:
    spelling = _text(value, label)
    path = Path(spelling)
    if path.is_absolute() or ".." in path.parts or path == Path(".") or any(ord(char) < 32 for char in spelling):
        raise Held("config", f"{label}: expected project-relative path")
    if path.parts[0] in (".git", "config.toml", ".gitignore", "README.md", "CONTRIBUTING.md"):
        raise Held("config", f"{label}: overlaps repository metadata")
    target = root / path
    try:
        resolved = target.resolve()
    except (OSError, RuntimeError) as error:
        raise Held("config", f"{label}: {error}") from error
    if not resolved.is_relative_to(root):
        raise Held("config", f"{label}: symlink escapes project")
    return target


def load_pending(root: Path) -> PendingProject:
    root = Path(root).expanduser().resolve()
    path = root / "config.toml"
    data = _read(path)
    schema = _required(data, "schema", "schema")
    if type(schema) is not int or schema != SCHEMA_VERSION:
        raise Held("config", f"schema: expected {SCHEMA_VERSION}")
    project = _table(data, "project", "project")
    workspace = _table(data, "workspace", "workspace")
    paths = _table(data, "paths", "paths")
    state = _required(project, "state", "project.state")
    if state not in ("awaiting-roms", "ready"):
        raise Held("config", "project.state: expected awaiting-roms or ready")
    if state == "awaiting-roms" and ("compilers" in data or "default_compiler" in project or "units" in data):
        raise Held("config", "project.state: pending config cannot contain compiler assignments")

    def identity(table: dict[str, Any], field: str) -> str:
        from uuid import UUID

        value = _text(_required(table, "id", field), field)
        try:
            if str(UUID(value)) != value:
                raise ValueError("expected canonical UUID")
        except ValueError as error:
            raise Held("config", f"{field}: expected canonical UUID") from error
        return value

    def structural(field: str) -> Path:
        return _relative(_required(paths, field, "paths." + field), "paths." + field, root)

    roms, build, work, drafts = (structural(field) for field in ("roms", "build", "work", "drafts"))
    src, asm, tools = (structural(field) for field in ("src", "asm", "tools"))
    includes = _strings(_required(paths, "include", "paths.include"), "paths.include")
    include = tuple(_relative(item, "paths.include", root) for item in includes)
    if not include:
        raise Held("config", "paths.include: expected nonempty array")
    if not work.is_relative_to(build) or work == build:
        raise Held("config", "paths.work: expected subtree of paths.build")
    if not drafts.is_relative_to(build) or drafts == build:
        raise Held("config", "paths.drafts: expected subtree of paths.build")
    protected = (roms, src, asm, tools, *include, root / "versions")
    for output in (build, work, drafts):
        for source in protected:
            if output.resolve().is_relative_to(source.resolve()) or source.resolve().is_relative_to(output.resolve()):
                raise Held("config", f"paths.build: overlaps protected path {source.relative_to(root)}")
    if work.resolve().is_relative_to(drafts.resolve()) or drafts.resolve().is_relative_to(work.resolve()):
        raise Held("config", "paths.work: overlaps paths.drafts")
    return PendingProject(
        root,
        identity(project, "project.id"),
        identity(workspace, "workspace.id"),
        state,
        roms,
        build,
        work,
        drafts,
        src,
        include,
        asm,
        tools,
    )


def load(root: Path) -> Project:
    root = Path(root).expanduser().resolve()
    path = root / "config.toml"
    pending = load_pending(root)
    if pending.state != "ready":
        raise Held("config", "project.state: awaiting-roms; run unbake setup")
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
        for mutable in (pending.work, pending.drafts):
            first = mutable.relative_to(pending.build).parts[0]
            if first == v or re.fullmatch(re.escape(v) + r"\.\d+", first):
                raise Held("config", "paths.work/paths.drafts: overlaps version generation")
    names_from = _text(value(project, "project", "names_from"), _label(path, "project", "names_from"))
    if names_from not in versions:
        raise Held("config", f"{_label(path, 'project', 'names_from')}: unknown VERSION {names_from}")

    def project_path(table: dict[str, Any], section: str, field: str) -> Path:
        return _relative(value(table, section, field), _label(path, section, field), root)

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
        cflags = _strings(_required(table, "cflags", label + ".cflags"), label + ".cflags")
        compilers[ident] = Compiler(
            ident,
            spec.kind,
            tools / ident / spec.cc,
            Path(spec.as_) if spec.as_.startswith("policy:") else tools / ident / spec.as_,
            cflags,
            tools / "compiler.sha256",
        )
    from unbake.project.compiler_ties import read as read_ties

    ties = read_ties(data.get("compiler_ties", {}), compilers)
    default_compiler = _text(value(project, "project", "default_compiler"), _label(path, "project", "default_compiler"))
    if default_compiler not in compilers and default_compiler not in ties:
        key = "compiler.tied_set" if default_compiler.startswith("tie:") else "unknown compiler"
        raise Held("config", f"{path} [project].default_compiler: {key}: {default_compiler}")
    units = {}
    for unit, ident in units_table.items():
        ident = _text(ident, f"{path} [units].{unit}")
        if ident not in compilers and ident not in ties:
            key = "compiler.tied_set" if ident.startswith("tie:") else "unknown compiler"
            raise Held("config", f"{path} [units].{unit}: {key}: {ident}")
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
            project_path(table, section, "baserom"),
            _digest(value(table, section, "baserom_sha1"), 40, _label(path, section, "baserom_sha1")),
            project_path(table, section, "split"),
            project_path(table, section, "symbols"),
            _strings(value(table, section, "macros"), _label(path, section, "macros")),
        )
    for version in version_map.values():
        if not version.baserom.is_relative_to(pending.roms):
            raise Held("config", f"version.{version.name}.baserom: expected path under paths.roms")
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
        pending.id,
        pending.workspace_id,
        pending.roms,
        pending.build,
        pending.work,
        pending.drafts,
        ties,
    )


def policy_path(path: Path | None = None) -> Path:
    if path is None:
        explicit = os.environ.get("UNBAKE_POLICY")
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
        path = Path(explicit) if explicit else base / "unbake" / "policy.toml"
    return path.expanduser().absolute()


def policy_template(path: Path) -> None:
    """Create an incomplete template without inventing host facts."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = (
        "setup_version_jobs",
        "symbol_similarity_threshold",
        "symbol_similarity_margin",
        "splat",
        "mips_as",
        "mips_ld",
        "mips_objcopy",
        "cpp",
        "cache_root",
        "state_root",
        "mips_objdump",
        "mips_readelf",
        "m2c",
        "objdiff_cli",
        "objdiff_sha256",
        "permuter_archive",
        "permuter_sha256",
        "asflags",
        "cppflags",
        "sn64_asflags",
    )
    with path.open("x") as output:
        output.write(POLICY_PATH.read_text())
        output.write("\n# Supply required host fields for the command you run.\n")
        for field in fields:
            output.write(f"# {field} = <required value>\n")


@overload
def load_policy(path: Path | None = None, *, stage: Literal["all"] = "all") -> Policy: ...


@overload
def load_policy(path: Path | None = None, *, stage: Literal["census"]) -> CensusPolicy: ...


@overload
def load_policy(path: Path | None = None, *, stage: Literal["setup"]) -> SetupPolicy: ...


def load_policy(path: Path | None = None, *, stage: str = "all") -> Policy | CensusPolicy | SetupPolicy:
    return _load_policy(path, stage=stage, create_template=True)


def read_policy(path: Path | None = None) -> Policy:
    """Read work policy for guidance without creating an absent host template."""
    return cast(Policy, _load_policy(path, stage="all", create_template=False))


def _load_policy(path: Path | None, *, stage: str, create_template: bool) -> Policy | CensusPolicy | SetupPolicy:
    path = policy_path(path)
    if not path.exists():
        if not create_template:
            raise Held("config", f"policy.path: supply host policy at {path}; run unbake setup")
        policy_template(path)
    data = {**_read(POLICY_PATH), **_read(path)}

    def value(field: str) -> Any:
        return _required(data, field, "policy." + field)

    def host_path(field: str) -> Path:
        return _path(value(field), "policy." + field, None)

    def executable(field: str) -> Path:
        target = host_path(field)
        if not target.is_file() or not os.access(target, os.X_OK):
            raise Held("config", f"policy.{field}: missing executable {target}")
        return target

    def number(field: str) -> int:
        return _positive(value(field), "policy." + field, integer=True)

    def flags(field: str) -> tuple[str, ...]:
        return _strings(value(field), "policy." + field)

    def fraction(field: str) -> float:
        result = value(field)
        if type(result) not in (int, float) or not 0 < result <= 1:
            raise Held("config", f"policy.{field}: expected number in (0, 1]")
        return float(result)

    similarity = value("same_game_similarity")
    if type(similarity) not in (int, float) or not 0 < similarity <= 1:
        raise Held("config", "policy.same_game_similarity: expected number in (0, 1]")
    if stage == "census":
        return CensusPolicy(float(similarity))
    if stage == "setup":
        return SetupPolicy(
            float(similarity),
            number("cores"),
            host_path("cache_root"),
            executable("splat"),
            executable("mips_as"),
            executable("mips_ld"),
            executable("mips_objcopy"),
            executable("cpp"),
            flags("asflags"),
            flags("cppflags"),
            flags("sn64_asflags"),
            number("probe_count"),
            number("setup_version_jobs"),
            fraction("symbol_similarity_threshold"),
            fraction("symbol_similarity_margin"),
        )
    if stage != "all":
        raise Held("config", f"policy.stage: unknown stage {stage}")
    return Policy(
        number("cores"),
        number("stall_trials"),
        number("search_beam"),
        _positive(value("assignment_idle_hours"), "policy.assignment_idle_hours", integer=False),
        host_path("cache_root"),
        host_path("state_root"),
        executable("objdiff_cli"),
        _digest(value("objdiff_sha256"), 64, "policy.objdiff_sha256"),
        executable("m2c"),
        executable("splat"),
        executable("mips_ld"),
        executable("mips_objdump"),
        executable("mips_readelf"),
        float(similarity),
        number("probe_count"),
        executable("mips_as"),
        executable("mips_objcopy"),
        executable("cpp"),
        flags("asflags"),
        flags("cppflags"),
        flags("sn64_asflags"),
        host_path("permuter_archive"),
        _digest(value("permuter_sha256"), 64, "policy.permuter_sha256"),
        number("setup_version_jobs"),
    )
