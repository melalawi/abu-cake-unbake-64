"""Pinned compiler registry, shared host installs ."""

from __future__ import annotations

import fcntl
import os
import platform
import re
import tarfile
import tempfile
import tomllib
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from unbake import atomic as atomic_files
from unbake import cache as retention
from unbake import inputs, tui
from unbake.compilers import files as compiler_files
from unbake.config import Held

if TYPE_CHECKING:
    from unbake.config import Host, Project

REGISTRY_PATH = Path(__file__).with_name("registry.toml")
MANIFEST = "compilers.sha256"


@dataclass(frozen=True)
class Download:
    url: str
    sha256: str
    files: tuple[str, ...]


@dataclass(frozen=True)
class CompilerSpec:
    id: str
    kind: str
    source: str
    host: str
    cc: str
    as_: str
    cflags: tuple[str, ...]
    pins: dict[str, str]
    downloads: tuple[Download, ...]
    family: str
    decompme: str
    splat: str
    m2c: str
    permuter: str


def _read(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as stream:
            return tomllib.load(stream)
    except (OSError, ValueError) as error:
        raise Held("setup", f"{path}: {error}") from error


def _required(table: dict[str, Any], name: str, label: str) -> Any:
    if not isinstance(table, dict) or name not in table:
        raise Held("setup", f"{label}.{name}: missing value")
    return table[name]


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise Held("setup", f"{label}: expected nonempty string")
    return value


def _target(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise Held("setup", f"{label}: expected target string (empty when unsupported)")
    return value


def _strings(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise Held("setup", f"{label}: expected array of strings")
    return tuple(_text(item, label) for item in value)


def _digest(value: object, label: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise Held("setup", f"{label}: expected lowercase SHA-256")
    return value


def _registry() -> dict[str, CompilerSpec]:
    """Read process data; no project facts or silent compiler choice."""
    tables = _required(_read(REGISTRY_PATH), "compilers", str(REGISTRY_PATH))
    if not isinstance(tables, dict) or not tables:
        raise Held("setup", f"{REGISTRY_PATH} [compilers]: expected nonempty table")
    result = {}
    for ident, table in tables.items():
        label = f"{REGISTRY_PATH} [compilers.{ident}]"
        if not re.fullmatch(r"[a-z0-9][a-z0-9.-]*", ident):
            raise Held("setup", f"{label}: unsafe compiler id")
        fields = {
            name: _text(_required(table, name, label), f"{label}.{name}")
            for name in ("kind", "source", "host", "cc", "as")
        }
        if not re.fullmatch(r"[a-z][a-z0-9_-]*", fields["kind"]):
            raise Held("setup", f"{label}.kind: expected safe execution-path name")
        if fields["source"] not in ("download", "supplied", "mixed"):
            raise Held("setup", f"{label}.source: expected download, supplied or mixed")
        pins = _required(table, "pins", label)
        if not isinstance(pins, dict) or not pins:
            raise Held("setup", f"{label}.pins: expected nonempty table")
        pins = {compiler_files.relative(name): _digest(value, f"{label}.pins.{name}") for name, value in pins.items()}
        for name in ("cc", "as"):
            if name == "as" and fields[name] == "policy:mips_as":
                continue
            if fields[name] not in pins:
                raise Held("setup", f"{label}.{name}: {fields[name]} has no file pin")
        downloads = []
        download_tables = _required(table, "downloads", label) if fields["source"] != "supplied" else []
        if not isinstance(download_tables, list) or (fields["source"] != "supplied" and not download_tables):
            raise Held("setup", f"{label}.downloads: expected nonempty array")
        covered: set[str] = set()
        for entry in download_tables:
            url = _text(_required(entry, "url", label + ".downloads"), label + ".downloads.url")
            if urllib.parse.urlsplit(url).scheme not in ("https", "file"):
                raise Held("setup", f"{label}.downloads.url: expected https:// or file://")
            sha = _digest(_required(entry, "sha256", label + ".downloads"), label + ".downloads.sha256")
            files = _strings(_required(entry, "files", label + ".downloads"), label + ".downloads.files")
            if not files or any(name not in pins or name in covered for name in files) or len(set(files)) != len(files):
                raise Held("setup", f"{label}.downloads.files: expected distinct pinned files")
            downloads.append(Download(url, sha, files))
            covered.update(files)
        if fields["source"] == "download" and covered != set(pins):
            raise Held("setup", f"{label}.downloads.files: missing {', '.join(sorted(set(pins) - covered))}")
        if fields["source"] == "mixed" and covered == set(pins):
            raise Held("setup", f"{label}.source: mixed requires supplied file pins")
        result[ident] = CompilerSpec(
            ident,
            fields["kind"],
            fields["source"],
            fields["host"],
            fields["cc"],
            fields["as"],
            _strings(_required(table, "cflags", label), label + ".cflags"),
            pins,
            tuple(downloads),
            _text(_required(table, "family", label), label + ".family"),
            _text(_required(table, "decompme", label), label + ".decompme"),
            _text(_required(table, "splat", label), label + ".splat"),
            _target(_required(table, "m2c", label), label + ".m2c"),
            _target(_required(table, "permuter", label), label + ".permuter"),
        )
    return result


def registry() -> dict[str, CompilerSpec]:
    from unbake import inputs
    from unbake.cache import memo

    return memo(
        "compiler.registry",
        (REGISTRY_PATH, inputs.signature(REGISTRY_PATH)),
        _registry,
        size=retention.memory_size,
        copy_out=retention.clone,
    )


def specification(ident: str) -> CompilerSpec:
    try:
        return registry()[ident]
    except KeyError as error:
        raise Held("setup", f"setup.compiler_candidate: {ident}: unknown registry id") from error


def acquire(spec: CompilerSpec, policy: Host | Host, *, supply: Path | None = None) -> Path:
    """Acquire a pinned candidate in the host cache without any project config."""
    if not policy.cache_machine_root.is_absolute():
        raise Held("setup", "policy.cache_machine_root: expected absolute path")
    current = specification(spec.id)
    if spec != current:
        raise Held("setup", f"setup.proposal_stale: compiler {spec.id}: registry specification changed")
    cache = policy.cache_machine_root / "compilers"
    cache.mkdir(parents=True, exist_ok=True)
    try:
        return _install(spec, cache, supply)
    except (OSError, ValueError, tarfile.TarError, zipfile.BadZipFile, urllib.error.URLError) as error:
        raise Held("setup", f"setup.compiler_candidate: {spec.id}: {error}") from error


def verify(directory: Path, spec: CompilerSpec) -> dict[str, str]:
    """Refuse every absent or changed pin by name, including on warm installs."""
    failures = []
    for name, expected in sorted(spec.pins.items()):
        path = directory / name
        try:
            actual = inputs.digest(path, algorithm="sha256", reuse=retention.configured())
        except OSError as error:
            failures.append(f"{path}: {error}")
            continue
        if actual != expected:
            failures.append(f"{path}: sha256 expected {expected}, found {actual}")
    if failures:
        raise Held("setup", "\n".join(failures))
    return dict(spec.pins)


def _hashes(directory: Path, spec: CompilerSpec) -> dict[str, str]:
    result = {}
    for name in spec.pins:
        path = directory / name
        if any(parent.is_symlink() for parent in path.parents):
            raise Held("setup", f"{path}: compiler parent is a symlink")
        if path.exists() or path.is_symlink():
            if not path.is_file():
                raise Held("setup", f"{path}: expected regular compiler file")
            result[name] = inputs.digest(path, algorithm="sha256", reuse=retention.configured())
    return result


def _replaced(path: Path, old: str | None, new: str) -> None:
    if old is not None and old != new:
        tui.line(f"REPLACED(setup): {path}: sha256 {old} -> {new}")


def _install(spec: CompilerSpec, cache: Path, source: Path | None, *, refresh: bool = False) -> Path:
    destination = cache / spec.id
    host = platform.system().lower() + "-" + platform.machine().lower()
    if host != spec.host:
        raise Held("setup", f"[compilers.{spec.id}].host: requires {spec.host}, found {host}")
    if destination.is_symlink():
        raise Held("setup", f"{destination}: host install must be a directory")
    with (cache / f".{spec.id}.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if destination.is_symlink():
            raise Held("setup", f"{destination}: host install must be a directory")
        if destination.exists() and not destination.is_dir():
            raise Held("setup", f"{destination}: host install must be a directory")
        previous = _hashes(destination, spec)
        if previous == spec.pins and not refresh:
            verify(destination, spec)
            return destination
        covered = {name for entry in spec.downloads for name in entry.files}
        supplied = {name: pin for name, pin in spec.pins.items() if name not in covered and previous.get(name) != pin}
        if supplied and source is None:
            raise Held(
                "setup",
                f"[compilers.{spec.id}].supply: missing archive/directory for {', '.join(sorted(supplied))}. "
                "Run setup --supply DIR with files matching the registry SHA-256 pins. "
                "See README Compilers for public downloads and proprietary files you must obtain yourself. "
                "Supplied files are not downloaded automatically.",
            )
        with tempfile.TemporaryDirectory(dir=cache, prefix=f".{spec.id}-") as temporary:
            stage = Path(temporary) / "install"
            stage.mkdir()
            contents = {
                pin: (destination / name).read_bytes()
                for name, pin in spec.pins.items()
                if name not in covered and previous.get(name) == pin
            }
            for entry in spec.downloads:
                wanted = {spec.pins[name] for name in entry.files}
                contents.update(compiler_files.archive_files(compiler_files.download(entry, cache), wanted))
            if supplied:
                assert source is not None
                contents.update(compiler_files.supplied_files(source, set(supplied.values())))
            for name, pin in spec.pins.items():
                if pin not in contents:
                    raise Held(
                        "setup",
                        f"[compilers.{spec.id}].pins.{name}: missing supplied/downloaded SHA-256 {pin}. "
                        "Supply the exact pinned file using setup --supply DIR. "
                        "See README Compilers for file acquisition. The supplied file must match this SHA-256.",
                    )
                target = stage / name
                target.parent.mkdir(parents=True, exist_ok=True)
                atomic_files.write(target, contents[pin])
                target.chmod(0o755)
            verify(stage, spec)
            if destination.exists():
                for name, pin in spec.pins.items():
                    target = destination / name
                    if previous.get(name) != pin or target.is_symlink():
                        target.parent.mkdir(parents=True, exist_ok=True)
                        atomic_files.publish(stage / name, target)
                        _replaced(target, previous.get(name), pin)
            else:
                os.replace(stage, destination)
            verify(destination, spec)
        return destination


def _supplies(project: Project, override: Path | None) -> dict[str, Path]:
    path = project.root / "config.toml"
    tables = _required(_read(path), "compilers", str(path))
    if not isinstance(tables, dict):
        raise Held("setup", f"{path} [compilers]: expected table")
    result = {}
    for ident in project.compilers:
        table = _required(tables, ident, f"{path} [compilers]")
        if not isinstance(table, dict):
            raise Held("setup", f"{path} [compilers.{ident}]: expected table")
        if override is not None:
            result[ident] = override
        elif "supply" in table:
            value = _text(table["supply"], f"{path} [compilers.{ident}].supply")
            source = Path(value).expanduser()
            result[ident] = source if source.is_absolute() else project.root / source
    return result


def _ensure(project: Project, policy: Host | Host, override: Path | None) -> Path:
    try:
        compilers, tools, root, cache_root = project.compilers, project.tools, project.root, policy.cache_machine_root
    except AttributeError as error:
        raise Held("setup", f"missing contract value {error}") from error
    if not isinstance(compilers, dict) or not compilers:
        raise Held("setup", "[compilers]: expected nonempty compiler set")
    if not cache_root.is_absolute():
        raise Held("setup", "policy.cache_machine_root: expected absolute path")
    try:
        tools.resolve().relative_to(root.resolve())
        tools_relative = tools.absolute().relative_to(root.absolute())
        compiler_files.relative(tools_relative.as_posix())
    except ValueError as error:
        raise Held("setup", f"tools/: {tools} must be inside {root}") from error
    specs = registry()
    for ident, compiler in compilers.items():
        if ident not in specs:
            raise Held("setup", f"[compilers.{ident}]: unknown registry id")
        try:
            compiler_id, kind = compiler.id, compiler.kind
        except AttributeError as error:
            raise Held("setup", f"[compilers.{ident}]: missing contract value {error}") from error
        if compiler_id != ident or kind != specs[ident].kind:
            raise Held("setup", f"[compilers.{ident}]: id/kind differs from registry")
    sources = _supplies(project, override)
    cache = cache_root / "compilers"
    cache.mkdir(parents=True, exist_ok=True)
    installs = {}
    for ident in sorted(compilers):
        project_directory = tools / ident
        if project_directory.is_symlink():
            raise Held("setup", f"{project_directory}: expected directory for compiler files")
        previous = _hashes(project_directory, specs[ident])
        refresh = any(pin != specs[ident].pins[name] for name, pin in previous.items())
        installs[ident] = _install(specs[ident], cache, sources.get(ident), refresh=refresh)
    tools.mkdir(parents=True, exist_ok=True)
    manifest_path = tools / MANIFEST
    if manifest_path.is_file():
        for line in manifest_path.read_text().splitlines():
            fields = line.split(maxsplit=1)
            if len(fields) != 2 or not re.fullmatch(r"[0-9a-f]{64}", fields[0]):
                continue
            relative = Path(compiler_files.relative(fields[1]))
            for ident in installs:
                prefix = tools_relative / ident
                if relative.is_relative_to(prefix) and relative.relative_to(prefix).as_posix() not in specs[ident].pins:
                    target = root / relative
                    if any(parent.is_symlink() for parent in target.parents):
                        raise Held("setup", f"{target}: compiler parent is a symlink")
                    if target.is_file() or target.is_symlink():
                        target.unlink()
    manifest = []
    for ident, directory in installs.items():
        project_directory = tools / ident
        if project_directory.is_symlink():
            raise Held("setup", f"{project_directory}: expected directory for compiler files")
        project_directory.mkdir(parents=True, exist_ok=True)
        for name, pin in sorted(specs[ident].pins.items()):
            target = project_directory / name
            if any(parent.is_symlink() for parent in target.parents):
                raise Held("setup", f"{target}: compiler parent is a symlink")
            target.parent.mkdir(parents=True, exist_ok=True)
            old = inputs.digest(target, algorithm="sha256", reuse=retention.configured()) if target.is_file() else None
            if old != pin or target.is_symlink():
                compiler_files.atomic_bytes(
                    target, (directory / name).read_bytes(), mode=(directory / name).stat().st_mode & 0o777
                )
                _replaced(target, old, pin)
            manifest.append(f"{pin}  {(tools_relative / ident / name).as_posix()}\n")
        verify(project_directory, specs[ident])
    compiler_files.atomic_bytes(manifest_path, "".join(manifest).encode())
    return manifest_path


def ensure(project: Project, policy: Host | Host, *, supply: Path | None = None) -> Path:
    """Install every declared compiler, verify every pin, copy files and return manifest."""
    try:
        return _ensure(project, policy, supply)
    except (OSError, ValueError, tarfile.TarError, zipfile.BadZipFile, urllib.error.URLError) as error:
        raise Held("setup", f"compiler setup: {error}") from error


def supply(project: Project, policy: Host | Host, source: Path) -> Path:
    """Explicit setup --supply input, shared by all requested compiler installs."""
    if source is None:
        raise Held("setup", "--supply: missing archive/directory")
    try:
        return _ensure(project, policy, Path(source).expanduser().absolute())
    except (OSError, ValueError, tarfile.TarError, zipfile.BadZipFile, urllib.error.URLError) as error:
        raise Held("setup", f"compiler supply {source}: {error}") from error


def status(policy: Host | Host) -> list[tuple[str, str]]:
    """Registry ids and verified installation state for setup --compilers."""
    rows = []
    for ident, spec in registry().items():
        try:
            verify(policy.cache_machine_root / "compilers" / ident, spec)
        except Held as error:
            rows.append((ident, error.reason))
        else:
            rows.append((ident, "installed; pins verified"))
    return rows
