"""Simultaneous canonical function/data-symbol renames with full-ROM proof."""

from __future__ import annotations

import difflib
import json
import re
import tomllib
from pathlib import Path
from typing import Any

from unbake.layout import name_transaction, split
from unbake.layout.name_transaction import Change
from unbake.project.build import BuildResult
from unbake.project.config import Held, Policy, Project


def read(path: Path) -> dict[str, str]:
    """Read an explicit JSON old->new object; duplicate keys are refused."""

    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise Held("split", f"split.rename.map: duplicate source name {key}")
            result[key] = value
        return result

    try:
        value = json.loads(path.read_bytes(), object_pairs_hook=pairs)
    except (OSError, ValueError) as error:
        raise Held("split", f"split.rename.map: {path}: {error}") from error
    if not isinstance(value, dict) or not value:
        raise Held("split", "split.rename.map: required nonempty JSON old->new name object")
    return validate(value)


def validate(mapping: dict[str, str]) -> dict[str, str]:
    targets: dict[str, str] = {}
    for old, new in mapping.items():
        split.name(old, "split.rename.old_name")
        split.name(new, f"split.rename.new_name.{old}")
        if old == new:
            raise Held("split", f"split.rename.map: {old}: old and new names must differ")
        if new in targets:
            raise Held("split", f"split.rename.conflict: {new}: targets both {targets[new]} and {old}")
        targets[new] = old
    return dict(mapping)


def path_name(value: str, mapping: dict[str, str]) -> str:
    return "/".join(mapping.get(part, part) for part in value.split("/"))


def source_names(text: str, mapping: dict[str, str]) -> str:
    """Rename identifier tokens, preserving comments and string/character bytes."""
    tokens = re.compile(r'/\*.*?\*/|//[^\n]*|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|(?P<name>\b[A-Za-z_]\w*\b)', re.S)
    return tokens.sub(lambda match: mapping.get(match["name"], match[0]) if match["name"] else match[0], text)


def _metadata(value: Any, mapping: dict[str, str]) -> Any:
    if isinstance(value, str):
        return path_name(value, mapping)
    if isinstance(value, list):
        return [_metadata(item, mapping) for item in value]
    if isinstance(value, dict):
        return {path_name(key, mapping): _metadata(item, mapping) for key, item in value.items()}
    return value


def plan(project: Project, mapping: dict[str, str]) -> list[Change]:
    """Plan against original names once, with no intermediate aliases or writes."""
    mapping = validate(mapping)
    changes: dict[Path, Change] = {}
    seen: set[str] = set()

    def add(path: Path, after: bytes | None) -> None:
        before = path.read_bytes() if path.exists() else None
        if before != after:
            changes[path] = Change(path, before, after)

    for version in project.versions:
        cartridge = project.version(version)
        _, lines, segments = split.layout(cartridge.split)
        symbols_before, symbols = split.symbols(cartridge.symbols)
        rows = [row for segment in segments for row in segment.rows]
        names = {row.path for row in rows}
        for row in rows:
            parts = row.path.split("/")
            seen.update(set(parts) & set(mapping))
            target = path_name(row.path, mapping)
            if target == row.path:
                continue
            if target in names and path_name(target, mapping) == target:
                raise Held("split", f"split.rename.conflict: {version}: row {target} already exists")
            lines[row.line] = split.replace_row(lines[row.line], row.match, path=target)
        final_paths = [path_name(row.path, mapping) for row in rows]
        # Structural rodata slices can share an owner but must keep distinct paths.
        if len(final_paths) != len(set(final_paths)):
            repeated = next(name for name in final_paths if final_paths.count(name) > 1)
            raise Held("split", f"split.rename.conflict: {version}: duplicate row {repeated}")
        add(cartridge.split, "".join(lines).encode())
        seen.update(set(symbols) & set(mapping))
        final_symbols: dict[str, tuple[int, str]] = {}
        rendered = []
        for line in symbols_before.splitlines(keepends=True):
            match = split.SYMBOL.match(line)
            if match is None:
                rendered.append(line)
                continue
            old = match["name"]
            new = mapping.get(old, old)
            address = int(match["address"], 0)
            if new in final_symbols:
                previous_address, previous_name = final_symbols[new]
                if address != previous_address:
                    raise Held(
                        "split",
                        f"split.rename.conflict: {version}: {new}: {previous_name} at {previous_address:#x} "
                        f"versus {old} at {address:#x}",
                    )
                # An existing equal-address alias is consolidated into one symbol,
                # rather than retaining the duplicate that Splat rejects.
                continue
            final_symbols[new] = address, old
            rendered.append(line[: match.start("name")] + new + line[match.end("name") :])
        add(cartridge.symbols, "".join(rendered).encode())
    missing = set(mapping) - seen
    if missing:
        raise Held("split", "split.rename.missing: " + ", ".join(sorted(missing)))
    config_path = project.root / "config.toml"
    config_text = config_path.read_text()
    data = tomllib.loads(config_text)
    units: dict[str, str] = {}
    for key, compiler in data.get("units", {}).items():
        path = Path(key)
        if path.suffix == ".c":
            renamed = str(path.with_name(mapping.get(path.stem, path.stem) + ".c"))
        else:
            renamed = path_name(key, mapping)
        if renamed in units and units[renamed] != compiler:
            raise Held("split", f"split.rename.conflict: compiler unit {renamed}")
        units[renamed] = compiler
    if units != data.get("units", {}):
        block = re.search(r"^\[units\][^\n]*\n(?P<body>.*?)(?=^\[|\Z)", config_text, re.M | re.S)
        if block is None:
            raise Held("split", "split.rename.config: missing [units] table")
        rendered_units = "".join(f"{json.dumps(key)} = {json.dumps(value)}\n" for key, value in units.items())
        add(
            config_path,
            (config_text[: block.start("body")] + rendered_units + config_text[block.end("body") :]).encode(),
        )
    # C/header contents use the same simultaneous token substitution. Moves
    # have original-byte snapshots, so cycles and rollback are well-defined.
    contents: dict[Path, bytes] = {}
    sources = sorted(project.src.rglob("*.c"))
    headers = sorted({path for root in project.include for path in root.rglob("*.h")})
    moved = set()
    for path in [*sources, *headers]:
        destination = path
        if path in sources and path.stem in mapping:
            destination = path.with_name(mapping[path.stem] + ".c")
            if destination.exists() and destination.stem not in mapping:
                raise Held("split", f"split.rename.conflict: source {destination} already exists")
            moved.add(path)
        contents[destination] = source_names(path.read_text(), mapping).encode()
    for path in moved - set(contents):
        add(path, None)
    for path, content in contents.items():
        add(path, content)
    # Setup's live owner manifests follow canonical names. Exact historical
    # compiler evidence is retained unchanged; map/solve detect changed inputs.
    metadata = [project.build / "setup/layout.json", project.root / "docs/setup/layout.json"]
    metadata.extend(project.root / "docs/setup" / (version + ".json") for version in project.versions)
    for path in metadata:
        if path.exists() and not path.is_relative_to(project.build):
            value = json.loads(path.read_bytes())
            renamed = _metadata(value, mapping)
            if renamed != value:
                add(path, (json.dumps(renamed, indent=2, sort_keys=True) + "\n").encode())
    return list(changes.values())


def diff(changes: list[Change]) -> str:
    return "".join(
        "".join(
            difflib.unified_diff(
                (change.before or b"").decode().splitlines(keepends=True),
                (change.after or b"").decode().splitlines(keepends=True),
                fromfile=str(change.path),
                tofile=str(change.path),
            )
        )
        for change in changes
    )


def apply(project: Project, policy: Policy, mapping: dict[str, str]) -> list[BuildResult]:
    return name_transaction.apply(project, policy, plan(project, mapping))
