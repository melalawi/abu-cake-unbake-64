"""The single generated declaration lookup, with safe index-listed paths."""

from __future__ import annotations

import json
import re
import tomllib
from functools import lru_cache
from pathlib import Path
from typing import Any

from unbake.config import Held, Project


def path(project: Project) -> Path:
    return project.build / "layout/index.json"


def load(project: Project) -> dict[str, Any]:
    target = path(project)
    if not target.is_file():
        return {"schema": 1, "symbols": {}, "clusters": {}, "headers": {}}
    try:
        value = _decoded(target, (target.stat().st_ino, target.stat().st_mtime_ns, target.stat().st_size))
        return dict(json.loads(json.dumps(value)))
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise Held("layout", f"layout.index: {target}: {error}") from error


@lru_cache(maxsize=32)
def _decoded(target: Path, stamp: tuple[int, int, int]) -> dict[str, Any]:
    try:
        value = json.loads(target.read_bytes())
        if not isinstance(value, dict) or set(value) not in (
            {"schema", "symbols", "clusters", "headers"},
            {"schema", "symbols", "clusters", "headers", "type_headers"},
        ):
            raise ValueError("invalid index keys")
        if value["schema"] != 1 or any(not isinstance(value[k], dict) for k in ("symbols", "clusters", "headers")):
            raise ValueError("invalid schema")
        for name, digest in value["headers"].items():
            safe(name)
            if not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
                raise ValueError("invalid header sha256")
        for name in (*value["symbols"].values(), *value["clusters"].values()):
            safe(name)
            if name not in value["headers"]:
                raise ValueError("unlisted declaration home")
        if not isinstance(value.get("type_headers", {}), dict):
            raise ValueError("invalid type catalogue")
        for homes in value.get("type_headers", {}).values():
            if not isinstance(homes, list):
                raise ValueError("invalid type homes")
            for home in homes:
                safe(home)
                if home not in value["headers"]:
                    raise ValueError("unlisted type home")
        return value
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise Held("layout", f"layout.index: {target}: {error}") from error


def safe(name: str) -> None:
    if not isinstance(name, str):
        raise ValueError(f"unsafe header path {name!r}")
    p = Path(name)
    if p.is_absolute() or ".." in p.parts or p.suffix != ".h":
        raise ValueError(f"unsafe header path {name!r}")


def headers(project: Project) -> frozenset[Path]:
    """The generated headers the index lists that exist: a header missing from a fresh or partial tree is not an
    input; the headers step regenerates it from the type solution."""
    return frozenset(path for path in _listed(project) if path.is_file())


def _listed(project: Project) -> frozenset[Path]:
    if not project.include:
        return frozenset()
    target = path(project)
    if not target.is_file():
        # A warm clone can omit the disposable index while retaining its
        # installed headers. Recover only guarded, map-owned generation homes;
        # treating them as authored would overwrite their consumed declarations.
        ownership = project.root / "layout.toml"
        if not ownership.is_file():
            return frozenset()
        stat = ownership.stat()
        return _unindexed_headers(project.include[0], ownership, (stat.st_ino, stat.st_mtime_ns, stat.st_size))
    stat = target.stat()
    names = tuple(_decoded(target, (stat.st_ino, stat.st_mtime_ns, stat.st_size))["headers"])
    return _headers(project.include[0], names, (stat.st_ino, stat.st_mtime_ns, stat.st_size))


@lru_cache(maxsize=32)
def _unindexed_headers(root: Path, ownership: Path, stamp: tuple[int, int, int]) -> frozenset[Path]:
    try:
        groups = tomllib.loads(ownership.read_text()).get("group", [])
        # common/types.h and <segment>/types.h are the pre-co-usage homes, found so they are removed.
        names = {"common/types.h", "common/data.h", "common/unused.h"}
        names.update(path.relative_to(root).as_posix() for path in (root / "common").glob("types_*.h"))
        for group in groups:
            segment, name = group["segment"], group["name"]
            names.update((f"{segment}/{name}.h", f"{segment}/types.h", f"{segment}/data.h"))
            # Inferred modules rename tool-named groups; their old headers are still generated output.
            names.update(
                path.relative_to(root).as_posix()
                for path in (root / segment).glob("code_*.h")
                if re.fullmatch(r"code_[0-9A-F]{8}\.h", path.name)
            )
        found = []
        for name in sorted(names):
            safe(name)
            path = root / name
            if not path.is_file():
                continue
            if not path.resolve().is_relative_to(root.resolve()):
                raise ValueError("header symlink escapes include root")
            guard = "UNBAKE_" + re.sub(r"[^A-Za-z0-9]", "_", name).upper()
            if re.match(r"\s*#\s*ifndef\s+" + guard + r"\s*\n\s*#\s*define\s+" + guard + r"\b", path.read_text()):
                found.append(path)
        return frozenset(found)
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise Held("layout", f"layout.index: unindexed installed headers: {error}") from error


@lru_cache(maxsize=32)
def _headers(root: Path, names: tuple[str, ...], stamp: tuple[int, int, int]) -> frozenset[Path]:
    result = frozenset(root / name for name in names)
    if any(not p.resolve().is_relative_to(root.resolve()) for p in result):
        raise Held("layout", "layout.index: header symlink escapes include root")
    return result


def encoded(value: dict[str, Any]) -> bytes:
    return (json.dumps(value, sort_keys=True, indent=2) + "\n").encode()


def update(project: Project, changes: dict[Path, str]) -> None:
    """Register explicit fold outputs; never rediscover headers by scanning."""
    import hashlib

    from unbake.cdecl import declarations
    from unbake.typemap import storage

    value = load(project)
    changed_names = {target.relative_to(project.include[0]).as_posix() for target in changes}
    value["symbols"] = {symbol: home for symbol, home in value["symbols"].items() if home not in changed_names}
    for target, text in changes.items():
        name = target.relative_to(project.include[0]).as_posix()
        safe(name)
        value["headers"][name] = hashlib.sha256(text.encode()).hexdigest()
        row = declarations(text)
        for symbol in row.typedefs | row.declared | row.exports:
            value["symbols"][symbol] = name
    storage.write(path(project), encoded(value))
