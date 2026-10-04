"""Compose proved text placements into reviewable layout edits."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from unbake.decomp.needs import Need, PlacementNeed
from unbake.layout import split, split_edits
from unbake.config import Held
from unbake import atomic as atomic_files


def _rename(project: Any, need: PlacementNeed) -> list[split.Edit]:
    version = project.version(need.version)
    before, lines, segments = split.layout(version.split)
    rows = [
        row
        for segment in segments
        for row in segment.rows
        if Path(row.path).name == need.value and row.start == need.start
    ]
    if len(rows) != 1 or rows[0].kind != "asm" or split.end(rows[0]) != need.end:
        raise Held("placement", f"{need.function} rename: missing proved assembly row {need.value}")
    row = rows[0]
    address = split.address(row, version.split)
    symbols_before, symbols = split.symbols(version.symbols)
    canonical = symbols.get(need.function)
    if canonical is not None and canonical[0] != address:
        raise Held("placement", f"{need.function} rename: conflicting canonical address")
    if any(
        Path(other.path).name == need.function for segment in segments for other in segment.rows if other is not row
    ):
        raise Held("placement", f"{need.function} rename: already has a row")
    lines[row.line] = split.replace_row(lines[row.line], row.match, path=str(Path(row.path).with_name(need.function)))
    edits = [split.Edit(version.split, before, "".join(lines), (need.version,))]
    old = symbols.get(cast(str, need.value))
    if old is not None:
        symbol_lines = symbols_before.splitlines(keepends=True)
        if canonical is not None:
            symbol_lines[old[1]] = ""
        else:
            match = old[2]
            line = symbol_lines[old[1]]
            symbol_lines[old[1]] = line[: match.start("name")] + need.function + line[match.end("name") :]
        edits.append(split.Edit(version.symbols, symbols_before, "".join(symbol_lines), (need.version,)))
    else:
        edits.extend(split_edits.place(project, need.version, need.function, address))
    return edits


def resolve(needs: list[Need], project: Any, policy: Any) -> list[split.Edit]:
    """Compose placement needs into edits without writing project files."""
    from tempfile import TemporaryDirectory
    from types import SimpleNamespace

    from unbake.decomp.needs import PlacementNeed

    selected = [need for need in needs if isinstance(need, PlacementNeed)]
    if policy is None:
        raise Held("placement", "policy: required")
    if not selected:
        return []
    # The temporary view lets successive cuts and renames share one edit base.
    with TemporaryDirectory(prefix="placement-") as temporary:
        root = Path(temporary)
        versions = {}
        originals = {}
        for version in dict.fromkeys(need.version for need in selected):
            original = project.version(version)
            directory = root / version
            directory.mkdir()
            for field in ("split", "symbols"):
                path = getattr(original, field)
                destination = directory / Path(path).name
                atomic_files.copyfile(path, destination)
                originals[destination] = (Path(path), split.read(path), version)
            versions[version] = SimpleNamespace(
                split=directory / Path(original.split).name, symbols=directory / Path(original.symbols).name
            )
        view: Any = SimpleNamespace(version=lambda version: versions[version])
        for need in selected:
            if need.value is None:
                raise Held("placement", f"{need.function} {need.action}.value: required")
            if need.action == "cut":
                edits = split_edits.cut(view, need.version, need.function, need.start, need.end)
            elif need.action == "rename":
                edits = _rename(view, need)
            elif need.action == "place":
                edits = split_edits.place(view, need.version, need.function, need.value)
            elif need.action == "align":
                edits = split_edits.align(view, need.version, need.function, need.value)
            else:
                raise Held("placement", f"{need.function} action {need.action}: unsupported")
            for edit in edits:
                atomic_files.text(edit.path, edit.after)
        return [
            split.Edit(original, before, split.read(path), (version,))
            for path, (original, before, version) in originals.items()
            if before != split.read(path)
        ]
