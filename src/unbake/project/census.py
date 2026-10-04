"""Inventory ROM inputs without compiler selection or ready publication."""

from __future__ import annotations

import hashlib
import json
import sys
import tomllib
import zlib
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from unbake.layout import split_analysis
from unbake.layout.split import Function
from unbake.project import compiler_files, rom
from unbake.project.config import CensusPolicy, Held, PendingProject


@dataclass(frozen=True)
class Census:
    cartridges: tuple[rom.Rom, ...]
    names: dict[Path, str]
    names_from: str
    inventories: dict[Path, tuple[Function, ...]]
    matrix: dict[tuple[rom.Rom, rom.Rom], float]
    manifest: Path

    @property
    def versions(self) -> tuple[str, ...]:
        return tuple(self.names[cartridge.path] for cartridge in self.cartridges)


def ingest_manifest(project: PendingProject) -> dict[str, Any] | None:
    manifest = project.build / "setup/roms.json"
    if manifest.is_symlink() or any(parent.is_symlink() for parent in manifest.parents):
        raise Held("setup", f"setup.roms.manifest: {manifest}: symlink")
    if not manifest.is_file():
        return None
    try:
        previous = json.loads(manifest.read_text())
        if (
            not isinstance(previous, dict)
            or type(previous.get("schema")) is not int
            or previous.get("schema") != 1
            or previous.get("project_id") != project.id
        ):
            raise ValueError("incompatible manifest")
        if previous.get("ingestion_complete") is not True:
            raise ValueError("incomplete manifest")
        if not isinstance(previous["generated_inputs"], list) or not isinstance(previous["renames"], dict):
            raise ValueError("invalid generated inputs or renames")
        if not isinstance(previous["names_from"], str) or not previous["names_from"]:
            raise ValueError("invalid names_from")
        order = previous["version_order"]
        if order is not None and (not isinstance(order, list) or any(not isinstance(value, str) for value in order)):
            raise ValueError("invalid version order")
        return previous
    except (ValueError, KeyError, TypeError) as error:
        raise Held("setup", f"setup.roms.manifest: {manifest}: {error}") from error


def candidates(project: PendingProject) -> list[Path]:
    """Ignore only unchanged generated copies recorded by the prior census."""
    if not project.roms.is_dir() or project.roms.is_symlink():
        raise Held("setup", f"setup.roms: supply ROMs in {project.roms}")
    previous = ingest_manifest(project)
    generated: dict[Path, str] = {}
    if previous is not None:
        try:
            for row in previous["generated_inputs"]:
                source_relative = Path(row["original_path"])
                target_relative = Path(row["normalized_path"])
                if any(path.is_absolute() or ".." in path.parts for path in (source_relative, target_relative)):
                    raise ValueError("expected project-relative ROM paths")
                source = project.root / source_relative
                target = project.root / target_relative
                if source.parent != project.roms or target.parent != project.roms:
                    raise ValueError("ROM paths must be direct children of paths.roms")
                if source != target and source.is_file() and not source.is_symlink():
                    generated[target] = row["sha1"]
        except (ValueError, KeyError, TypeError) as error:
            raise Held("setup", f"setup.roms.manifest: {project.build / 'setup/roms.json'}: {error}") from error
    paths = []
    for path in sorted(project.roms.iterdir()):
        if path.is_symlink():
            raise Held("setup", f"setup.roms: {path}: symlink input")
        if not path.is_file():
            continue
        if path in generated and hashlib.sha1(path.read_bytes()).hexdigest() == generated[path]:
            continue
        paths.append(path)
    if not paths:
        raise Held("setup", f"setup.roms: supply ROMs in {project.roms}")
    return paths


def measured_code(cartridge: rom.Rom) -> tuple[Function, ...]:
    """Reuse independently measured loaded bounds and control-flow extents."""
    data = cartridge.image()
    spans = split_analysis.copied_text(data)
    if not spans:
        bounds = split_analysis.loaded_bounds(data)
        if bounds is None:
            raise Held("setup", f"setup.same_game.code_ranges: {cartridge.path}: loaded mapping missing")
        bias = cartridge.header.entry - 0x1000
        limit = bounds[0] - bias
        end = split_analysis.executable_end(data, 0x1000, limit, bias, measured_end=limit)
        if not 0x1000 < end <= limit:
            raise Held("setup", f"setup.same_game.code_ranges: {cartridge.path}: invalid executable extent")
        spans = [(0x1000, end, bias)]
    return tuple(
        Function("census", f"text_{start:X}", start, end, start + bias, "census", "asm", ())
        for start, end, bias in spans
    )


def naming_version(versions: tuple[str, ...], selected: str | None) -> str:
    """Require an explicit choice, including a single-version project."""
    if not versions:
        raise Held("setup", "project.versions: no versions")
    if selected is None and sys.stdin.isatty():
        try:
            selected = input(f"Which version names functions? ({', '.join(versions)}): ").strip()
        except EOFError:
            selected = None
    if not selected:
        raise Held("setup", f"project.names_from: supply --names-from VERSION; valid values: {', '.join(versions)}")
    if selected not in versions:
        raise Held("setup", f"project.names_from: unknown VERSION {selected}; valid values: {', '.join(versions)}")
    return selected


def run(
    project: PendingProject,
    policy: CensusPolicy,
    *,
    names_from: str | None,
    renames: dict[str, str] | None = None,
    order: tuple[str, ...] | None = None,
) -> Census:
    cartridges = [rom.load(path, retain_data=False) for path in candidates(project)]
    seen: dict[str, Path] = {}
    for cartridge in cartridges:
        if cartridge.sha1 in seen:
            raise Held(
                "setup",
                f"setup.roms.duplicate_sha1: {cartridge.path}: duplicate sha1 "
                f"{cartridge.sha1} ({seen[cartridge.sha1]})",
            )
        seen[cartridge.sha1] = cartridge.path
    if project.state == "ready":
        with (project.root / "config.toml").open("rb") as source:
            pinned = tomllib.load(source)
        expected_digests = {pinned["version"][name]["baserom_sha1"] for name in pinned["project"]["versions"]}
        if set(seen) != expected_digests or len(cartridges) != len(expected_digests):
            raise Held("setup", "setup.rom_set_changed: ROM set differs from the ready project")
    # Identity has precedence over label collisions between unrelated games.
    codes = {(cartridge.header.category, cartridge.header.game_code) for cartridge in cartridges}
    if len(codes) != 1:
        facts = ", ".join(f"{item.path.name}={item.header.category + item.header.game_code}" for item in cartridges)
        raise Held("setup", f"setup.same_game.game_code: mixed games: {facts}")
    previous = ingest_manifest(project)
    if renames is None:
        renames = previous["renames"] if previous is not None else {}
    if order is None and previous is not None and previous["version_order"] is not None:
        saved_order = tuple(previous["version_order"])
        if set(saved_order) == set(rom.version_names(cartridges, renames).values()):
            order = saved_order
    names = rom.version_names(cartridges, renames)
    versions = tuple(sorted(names.values()))
    if order is not None:
        if len(order) != len(set(order)) or set(order) != set(versions):
            raise Held("setup", "project.versions: --version-order must list every VERSION once")
        versions = order
    by_version = {names[item.path]: item for item in cartridges}
    cartridges = [by_version[version] for version in versions]
    inventories = {item.path: measured_code(item) for item in cartridges}
    matrix = rom.similarity_matrix(cartridges, inventories)
    for item in cartridges:
        label = names[item.path]
        print(
            f"OK(setup): ROM {item.path.name}: VERSION {label} title={item.header.title!r} "
            f"code={item.header.category + item.header.game_code + item.header.region} "
            f"revision={item.header.revision} CIC={item.header.cic} sha1={item.sha1}"
        )
        print(
            f"OK(setup): same-game {label}: "
            + " ".join(f"{names[other.path]}={matrix[item, other]:.6f}" for other in cartridges)
        )
    selected = naming_version(versions, names_from)
    manifest = project.build / "setup/roms.json"
    rows = []
    for item in cartridges:
        label = names[item.path]
        target = project.roms / f"baserom.{label}.z64"
        rows.append(
            {
                "original_path": item.path.relative_to(project.root).as_posix(),
                "normalized_path": target.relative_to(project.root).as_posix(),
                "version": label,
                "sha1": item.sha1,
                "header": asdict(item.header),
                "crc_validated": True,
                "ipl3_crc32": f"{zlib.crc32(item.image()[0x40:0x1000]):08x}",
                "code_ranges": [
                    {"start": row.start, "end": row.end, "address": row.address} for row in inventories[item.path]
                ],
            }
        )
    generated_inputs = (
        {row["normalized_path"]: row for row in previous["generated_inputs"]} if previous is not None else {}
    )
    for row in rows:
        if row["original_path"] != row["normalized_path"]:
            generated_inputs[row["normalized_path"]] = {
                key: row[key] for key in ("original_path", "normalized_path", "sha1")
            }
    document = {
        "schema": 1,
        "ingestion_complete": False,
        "project_id": project.id,
        "names_from": selected,
        "renames": renames,
        "version_order": list(order) if order is not None else None,
        "generated_inputs": [generated_inputs[key] for key in sorted(generated_inputs)],
        "versions": list(versions),
        "roms": rows,
        "same_game_similarity": policy.same_game_similarity,
        "matrix": {
            names[item.path]: {names[other.path]: matrix[item, other] for other in cartridges} for item in cartridges
        },
    }
    reference = by_version[selected]
    rom.same_game(cartridges, inventories, policy.same_game_similarity, reference=reference, matrix=matrix)
    if project.state == "ready":
        with (project.root / "config.toml").open("rb") as source:
            pinned = tomllib.load(source)
        expected = {
            name: values["baserom_sha1"]
            for name, values in pinned["version"].items()
            if name in pinned["project"]["versions"]
        }
        actual = {names[item.path]: item.sha1 for item in cartridges}
        if actual != expected:
            raise Held("setup", "setup.rom_set_changed: ROM set differs from the ready project")
    # Validate every destination before writing any normalized input.
    for item in cartridges:
        target = project.roms / f"baserom.{names[item.path]}.z64"
        image = item.image()
        if target == item.path and target.read_bytes() != image:
            raise Held("setup", f"setup.roms.destination: {target}: normalization would overwrite original input")
        if target.is_symlink() or (target.exists() and target.read_bytes() != image):
            raise Held("setup", f"setup.roms.destination: {target}: existing input differs")
    del image
    created = []
    try:
        for item in cartridges:
            target = project.roms / f"baserom.{names[item.path]}.z64"
            if not target.exists():
                compiler_files.atomic_bytes(target, item.image())
                created.append(target)
        document["ingestion_complete"] = True
        compiler_files.atomic_bytes(manifest, (json.dumps(document, indent=2, sort_keys=True) + "\n").encode())
    except BaseException:
        for target in created:
            target.unlink(missing_ok=True)
        raise
    return Census(tuple(cartridges), names, selected, inventories, matrix, manifest)
