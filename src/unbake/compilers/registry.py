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
from unbake.process import capture
from unbake.process import named as cause_named

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
    supported_options: tuple[str, ...]
    small_data: tuple[int, ...]
    isa: tuple[int, ...]


def _read(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as stream:
            return tomllib.load(stream)
    except (OSError, ValueError) as error:
        raise Held(
            capture(error, cause=cause_named(f"{path}", f"{path}: {error}", owner="compilers.registry", stage="setup"))
        ) from error


def _required(table: dict[str, Any], name: str, label: str) -> Any:
    if not isinstance(table, dict) or name not in table:
        raise Held(
            cause_named(f"{label}.{name}", f"{label}.{name}: missing value", owner="compilers.registry", stage="setup")
        )
    return table[name]


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise Held(
            cause_named(f"{label}", f"{label}: expected nonempty string", owner="compilers.registry", stage="setup")
        )
    return value


def _target(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise Held(
            cause_named(
                f"{label}",
                f"{label}: expected target string (empty when unsupported)",
                owner="compilers.registry",
                stage="setup",
            )
        )
    return value


def _strings(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise Held(
            cause_named(f"{label}", f"{label}: expected array of strings", owner="compilers.registry", stage="setup")
        )
    return tuple(_text(item, label) for item in value)


def _digest(value: object, label: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise Held(
            cause_named(f"{label}", f"{label}: expected lowercase SHA-256", owner="compilers.registry", stage="setup")
        )
    return value


def _registry() -> dict[str, CompilerSpec]:
    """Read process data; no project facts or silent compiler choice."""
    tables = _required(_read(REGISTRY_PATH), "compilers", str(REGISTRY_PATH))
    if not isinstance(tables, dict) or not tables:
        raise Held(
            cause_named(
                "compilers.registry._registry",
                f"{REGISTRY_PATH} [compilers]: expected nonempty table",
                owner="compilers.registry",
                stage="setup",
            )
        )
    result = {}
    for ident, table in tables.items():
        label = f"{REGISTRY_PATH} [compilers.{ident}]"
        if not re.fullmatch(r"[a-z0-9][a-z0-9.-]*", ident):
            raise Held(
                cause_named(f"{label}", f"{label}: unsafe compiler id", owner="compilers.registry", stage="setup")
            )
        fields = {
            name: _text(_required(table, name, label), f"{label}.{name}")
            for name in ("kind", "source", "host", "cc", "as")
        }
        if not re.fullmatch(r"[a-z][a-z0-9_-]*", fields["kind"]):
            raise Held(
                cause_named(
                    f"{label}.kind",
                    f"{label}.kind: expected safe execution-path name",
                    owner="compilers.registry",
                    stage="setup",
                )
            )
        if fields["source"] not in ("download", "supplied", "mixed"):
            raise Held(
                cause_named(
                    f"{label}.source",
                    f"{label}.source: expected download, supplied or mixed",
                    owner="compilers.registry",
                    stage="setup",
                )
            )
        pins = _required(table, "pins", label)
        if not isinstance(pins, dict) or not pins:
            raise Held(
                cause_named(
                    f"{label}.pins", f"{label}.pins: expected nonempty table", owner="compilers.registry", stage="setup"
                )
            )
        pins = {compiler_files.relative(name): _digest(value, f"{label}.pins.{name}") for name, value in pins.items()}
        for name in ("cc", "as"):
            if name == "as" and fields[name] == "policy:mips_as":
                continue
            if fields[name] not in pins:
                raise Held(
                    cause_named(
                        f"{label}.{name}",
                        f"{label}.{name}: {fields[name]} has no file pin",
                        owner="compilers.registry",
                        stage="setup",
                    )
                )
        downloads = []
        download_tables = _required(table, "downloads", label) if fields["source"] != "supplied" else []
        if not isinstance(download_tables, list) or (fields["source"] != "supplied" and not download_tables):
            raise Held(
                cause_named(
                    f"{label}.downloads",
                    f"{label}.downloads: expected nonempty array",
                    owner="compilers.registry",
                    stage="setup",
                )
            )
        covered: set[str] = set()
        for entry in download_tables:
            url = _text(_required(entry, "url", label + ".downloads"), label + ".downloads.url")
            if urllib.parse.urlsplit(url).scheme not in ("https", "file"):
                raise Held(
                    cause_named(
                        f"{label}.downloads.url",
                        f"{label}.downloads.url: expected https:// or file://",
                        owner="compilers.registry",
                        stage="setup",
                    )
                )
            sha = _digest(_required(entry, "sha256", label + ".downloads"), label + ".downloads.sha256")
            files = _strings(_required(entry, "files", label + ".downloads"), label + ".downloads.files")
            if not files or any(name not in pins or name in covered for name in files) or len(set(files)) != len(files):
                raise Held(
                    cause_named(
                        f"{label}.downloads.files",
                        f"{label}.downloads.files: expected distinct pinned files",
                        owner="compilers.registry",
                        stage="setup",
                    )
                )
            downloads.append(Download(url, sha, files))
            covered.update(files)
        if fields["source"] == "download" and covered != set(pins):
            raise Held(
                cause_named(
                    f"{label}.downloads.files",
                    f"{label}.downloads.files: missing {', '.join(sorted(set(pins) - covered))}",
                    owner="compilers.registry",
                    stage="setup",
                )
            )
        if fields["source"] == "mixed" and covered == set(pins):
            raise Held(
                cause_named(
                    f"{label}.source",
                    f"{label}.source: mixed requires supplied file pins",
                    owner="compilers.registry",
                    stage="setup",
                )
            )
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
            _strings(_required(table, "supported_options", label), label + ".supported_options"),
            tuple(table["small_data"]),
            tuple(table["isa"]),
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
        raise Held(
            capture(
                error,
                cause=cause_named(
                    "setup.compiler_candidate",
                    f"setup.compiler_candidate: {ident}: unknown registry id",
                    owner="compilers.registry",
                    stage="setup",
                ),
            )
        ) from error


def acquire(spec: CompilerSpec, policy: Host | Host, *, supply: Path | None = None) -> Path:
    """Acquire a pinned candidate in the host cache without any project config."""
    if not policy.cache_machine_root.is_absolute():
        raise Held(
            cause_named(
                "policy.cache_machine_root",
                "policy.cache_machine_root: expected absolute path",
                owner="compilers.registry",
                stage="setup",
            )
        )
    current = specification(spec.id)
    if spec != current:
        raise Held(
            cause_named(
                "setup.proposal_stale",
                f"setup.proposal_stale: compiler {spec.id}: registry specification changed",
                owner="compilers.registry",
                stage="setup",
            )
        )
    cache = policy.cache_machine_root / "compilers"
    cache.mkdir(parents=True, exist_ok=True)
    try:
        return _install(spec, cache, supply)
    except (OSError, ValueError, tarfile.TarError, zipfile.BadZipFile, urllib.error.URLError) as error:
        raise Held(
            capture(
                error,
                cause=cause_named(
                    "setup.compiler_candidate",
                    f"setup.compiler_candidate: {spec.id}: {error}",
                    owner="compilers.registry",
                    stage="setup",
                ),
            )
        ) from error


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
        raise Held(
            cause_named("compilers.registry.verify", "\n".join(failures), owner="compilers.registry", stage="setup")
        )
    return dict(spec.pins)


def _hashes(directory: Path, spec: CompilerSpec) -> dict[str, str]:
    result = {}
    for name in spec.pins:
        path = directory / name
        if any(parent.is_symlink() for parent in path.parents):
            raise Held(
                cause_named(
                    f"{path}", f"{path}: compiler parent is a symlink", owner="compilers.registry", stage="setup"
                )
            )
        if path.exists() or path.is_symlink():
            if not path.is_file():
                raise Held(
                    cause_named(
                        f"{path}", f"{path}: expected regular compiler file", owner="compilers.registry", stage="setup"
                    )
                )
            result[name] = inputs.digest(path, algorithm="sha256", reuse=retention.configured())
    return result


def _replaced(path: Path, old: str | None, new: str) -> None:
    if old is not None and old != new:
        tui.line(f"REPLACED(setup): {path}: sha256 {old} -> {new}")


def _install(spec: CompilerSpec, cache: Path, source: Path | None, *, refresh: bool = False) -> Path:
    from unbake.compilers.recipe_options import recipe_digest

    destination = cache / (spec.id + "-" + recipe_digest({"pins": spec.pins, "host": spec.host}))
    host = platform.system().lower() + "-" + platform.machine().lower()
    if host != spec.host:
        raise Held(
            cause_named(
                f"[compilers.{spec.id}].host",
                f"[compilers.{spec.id}].host: requires {spec.host}, found {host}",
                owner="compilers.registry",
                stage="setup",
            )
        )
    if destination.is_symlink():
        raise Held(
            cause_named(
                f"{destination}",
                f"{destination}: host install must be a directory",
                owner="compilers.registry",
                stage="setup",
            )
        )
    with (cache / f".{spec.id}.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if destination.is_symlink():
            raise Held(
                cause_named(
                    f"{destination}",
                    f"{destination}: host install must be a directory",
                    owner="compilers.registry",
                    stage="setup",
                )
            )
        if destination.exists() and not destination.is_dir():
            raise Held(
                cause_named(
                    f"{destination}",
                    f"{destination}: host install must be a directory",
                    owner="compilers.registry",
                    stage="setup",
                )
            )
        if destination.exists():
            verify(destination, spec)
            return destination
        previous: dict[str, str] = {}
        covered = {name for entry in spec.downloads for name in entry.files}
        supplied = {name: pin for name, pin in spec.pins.items() if name not in covered and previous.get(name) != pin}
        if supplied and source is None:
            raise Held(
                cause_named(
                    f"[compilers.{spec.id}].supply",
                    (
                        f"[compilers.{spec.id}].supply: missing archive/directory for "
                        f"{', '.join(sorted(supplied))}. Run setup --supply DIR with files "
                        f"matching the registry SHA-256 pins. See README Compilers for public "
                        f"downloads and proprietary files you must obtain yourself. Supplied files "
                        f"are not downloaded automatically."
                    ),
                    owner="compilers.registry",
                    stage="setup",
                )
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
                        cause_named(
                            f"[compilers.{spec.id}].pins.{name}",
                            (
                                f"[compilers.{spec.id}].pins.{name}: missing supplied/downloaded "
                                f"SHA-256 {pin}. Supply the exact pinned file using setup --supply DIR. "
                                f"See README Compilers for file acquisition. The supplied file must match "
                                f"this SHA-256."
                            ),
                            owner="compilers.registry",
                            stage="setup",
                        )
                    )
                target = stage / name
                target.parent.mkdir(parents=True, exist_ok=True)
                atomic_files.write(target, contents[pin])
                target.chmod(0o755)
            verify(stage, spec)
            os.replace(stage, destination)
            verify(destination, spec)
        return destination


def _supplies(project: Project, override: Path | None) -> dict[str, Path]:
    path = project.root / "config.toml"
    tables = _required(_read(path), "compilers", str(path))
    if not isinstance(tables, dict):
        raise Held(
            cause_named(
                "compilers.registry._supplies",
                f"{path} [compilers]: expected table",
                owner="compilers.registry",
                stage="setup",
            )
        )
    result = {}
    for ident in project.compilers:
        table = _required(tables, ident, f"{path} [compilers]")
        if not isinstance(table, dict):
            raise Held(
                cause_named(
                    "compilers.registry._supplies",
                    f"{path} [compilers.{ident}]: expected table",
                    owner="compilers.registry",
                    stage="setup",
                )
            )
        if override is not None:
            result[ident] = override
        elif "supply" in table:
            value = _text(table["supply"], f"{path} [compilers.{ident}].supply")
            source = Path(value).expanduser()
            result[ident] = source if source.is_absolute() else project.root / source
    return result


def compiler_directory(tools: Path, spec: CompilerSpec) -> Path:
    """One immutable binary directory per exact pin set and host."""
    from unbake.compilers.recipe_options import recipe_digest

    return tools / (spec.id + "-" + recipe_digest({"pins": spec.pins, "host": spec.host}))


def _ensure(project: Project, policy: Host | Host, override: Path | None) -> Path:
    try:
        compilers, tools, root, cache_root = project.compilers, project.tools, project.root, policy.cache_machine_root
    except AttributeError as error:
        raise Held(
            capture(
                error,
                cause=cause_named(
                    "compilers.registry._ensure",
                    f"missing contract value {error}",
                    owner="compilers.registry",
                    stage="setup",
                ),
            )
        ) from error
    if not isinstance(compilers, dict) or not compilers:
        raise Held(
            cause_named(
                "[compilers]", "[compilers]: expected nonempty compiler set", owner="compilers.registry", stage="setup"
            )
        )
    if not cache_root.is_absolute():
        raise Held(
            cause_named(
                "policy.cache_machine_root",
                "policy.cache_machine_root: expected absolute path",
                owner="compilers.registry",
                stage="setup",
            )
        )
    try:
        tools.resolve().relative_to(root.resolve())
        tools_relative = tools.absolute().relative_to(root.absolute())
        compiler_files.relative(tools_relative.as_posix())
    except ValueError as error:
        raise Held(
            capture(
                error,
                cause=cause_named(
                    "tools/", f"tools/: {tools} must be inside {root}", owner="compilers.registry", stage="setup"
                ),
            )
        ) from error
    specs = registry()
    for ident, compiler in compilers.items():
        if ident not in specs:
            raise Held(
                cause_named(
                    f"[compilers.{ident}]",
                    f"[compilers.{ident}]: unknown registry id",
                    owner="compilers.registry",
                    stage="setup",
                )
            )
        try:
            compiler_id, kind = compiler.id, compiler.kind
        except AttributeError as error:
            raise Held(
                capture(
                    error,
                    cause=cause_named(
                        f"[compilers.{ident}]",
                        f"[compilers.{ident}]: missing contract value {error}",
                        owner="compilers.registry",
                        stage="setup",
                    ),
                )
            ) from error
        if compiler_id != ident or kind != specs[ident].kind:
            raise Held(
                cause_named(
                    f"[compilers.{ident}]",
                    f"[compilers.{ident}]: id/kind differs from registry",
                    owner="compilers.registry",
                    stage="setup",
                )
            )
    sources = _supplies(project, override)
    cache = cache_root / "compilers"
    cache.mkdir(parents=True, exist_ok=True)
    tools.mkdir(parents=True, exist_ok=True)
    manifest = []
    for ident in sorted(compilers):
        spec = specs[ident]
        project_directory = compiler_directory(tools, spec)
        if project_directory.exists():
            verify(project_directory, spec)
        else:
            directory = _install(spec, cache, sources.get(ident), refresh=False)
            with tempfile.TemporaryDirectory(prefix=".compiler-", dir=tools) as temporary:
                stage = Path(temporary) / "release"
                stage.mkdir()
                for name in spec.pins:
                    target = stage / name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    atomic_files.write(
                        target, (directory / name).read_bytes(), mode=(directory / name).stat().st_mode & 0o777
                    )
                verify(stage, spec)
                os.replace(stage, project_directory)
        for name, pin in sorted(spec.pins.items()):
            manifest.append(f"{pin}  {(project_directory / name).relative_to(root).as_posix()}\n")
    manifest_path = tools / MANIFEST
    atomic_files.write(manifest_path, "".join(manifest).encode())
    return manifest_path


def ensure(project: Project, policy: Host | Host, *, supply: Path | None = None) -> Path:
    """Install every declared compiler, verify every pin, copy files and return manifest."""
    try:
        return _ensure(project, policy, supply)
    except (OSError, ValueError, tarfile.TarError, zipfile.BadZipFile, urllib.error.URLError) as error:
        raise Held(
            capture(
                error,
                cause=cause_named(
                    "compilers.registry.ensure", f"compiler setup: {error}", owner="compilers.registry", stage="setup"
                ),
            )
        ) from error


def supply(project: Project, policy: Host | Host, source: Path) -> Path:
    """Explicit setup --supply input, shared by all requested compiler installs."""
    if source is None:
        raise Held(
            cause_named("--supply", "--supply: missing archive/directory", owner="compilers.registry", stage="setup")
        )
    try:
        return _ensure(project, policy, Path(source).expanduser().absolute())
    except (OSError, ValueError, tarfile.TarError, zipfile.BadZipFile, urllib.error.URLError) as error:
        raise Held(
            capture(
                error,
                cause=cause_named(
                    "compilers.registry.supply",
                    f"compiler supply {source}: {error}",
                    owner="compilers.registry",
                    stage="setup",
                ),
            )
        ) from error


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
