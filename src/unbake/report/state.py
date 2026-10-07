"""Current source publication state, independent of historical attempt maxima."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from unbake import cdecl, inputs, strict_json
from unbake.config import Held, Project
from unbake.fold import source_views
from unbake.layout import split
from unbake.work import attempts


@dataclass
class Inventory:
    units: dict[str, list[split.Function]]
    receipts: dict[str, dict[str, Any]]
    sources: dict[str, tuple[str, set[str], bool]]
    definitions: dict[tuple[str, str], set[str]]
    input_pins: dict[Path, inputs.Signature | None]


def input_signatures(project: Project) -> dict[Path, inputs.Signature | None]:
    from unbake.report import verify

    return {path: inputs.signature(path) if path.is_file() else None for path in verify.source_paths(project)}


def assert_current(project: Project, current: Inventory) -> None:
    if input_signatures(project) != current.input_pins:
        raise Held("report", "source.changed: current-source validation inputs changed; regenerate inventory")


def inventory(project: Project, *, receipts: dict[str, dict[str, Any]] | None = None) -> Inventory:
    """Read each version once, each retained source once, and receipts once."""
    pins = input_signatures(project)
    units = {version: split.functions(project, version) for version in project.versions}
    if receipts is None:
        # The publication owner replaces this current public input with the sole Ledger at hard cutover.
        for path in (attempts.summary_path(project), project.root / "unbake-original-asm.json"):
            if path.is_file():
                try:
                    strict_json.read(path)
                except ValueError as error:
                    raise Held("report", f"source.metadata: {error}") from error
        receipts = attempts.fuzzy_sources(project)
    sources = {}
    definitions = {}
    for path in sorted(project.src.rglob("*.c")):
        raw = path.read_bytes()
        text = raw.decode()
        # Comments cannot create a guard. The retained format has one whole-file guard.
        import re

        uncommented = re.sub(
            r'/\*.*?\*/|//[^\n]*|"(?:\\.|[^"\\])*"',
            lambda token: "\n" * token[0].count("\n") if token[0].startswith(("/*", "//")) else token[0],
            text,
            flags=re.S,
        )
        guarded = any(
            "NON_MATCHING" in line and line.lstrip().startswith(("#ifdef", "#if ", "#if("))
            for line in uncommented.splitlines()
        )
        if guarded:
            attempts.unguarded(text)
        unit = path.relative_to(project.src).with_suffix("").as_posix()
        names: set[str] = set()
        parsed_views: dict[str, set[str]] = {}
        for version in project.versions:
            view = source_views.version_source(project, text, version, unit, non_matching=guarded)
            clean = cdecl.declaration_source(view)
            if clean not in parsed_views:
                try:
                    parsed_views[clean] = cdecl.declarations(clean).functions
                except Held as error:
                    raise Held("report", f"source.syntax: {path} VERSION {version}: {error.reason}") from error
            definitions[unit, version] = parsed_views[clean]
            names.update(parsed_views[clean])
        sources[unit] = (hashlib.sha256(raw).hexdigest(), names, guarded)
    result = Inventory(units, receipts, sources, definitions, pins)
    validate(project, result)
    assert_current(project, result)
    return result


def validate(project: Project, current: Inventory) -> None:
    holdings: dict[str, set[str]] = {}
    for version, rows in current.units.items():
        for row in rows:
            source = current.sources.get(row.path)
            if row.kind == "c":
                if source is None or source[2]:
                    raise Held(
                        "report", f"source.state: VERSION {version} {row.path}: exact C source missing or guarded"
                    )
                for member in split.unit_members(row):
                    if not set(member.aliases) & current.definitions[row.path, version]:
                        raise Held("report", f"source.membership: VERSION {version} {member.name}: C body missing")
            for member in split.unit_members(row):
                for name in member.aliases:
                    holdings.setdefault(name, set()).add(version)
    for path, (_, names, guarded) in current.sources.items():
        if not guarded:
            continue
        if path not in current.receipts:
            raise Held(
                "report",
                f"source.receipt: src/{path}.c: retained source has no receipt; reconcile verified prior evidence",
            )
        if path not in names:
            raise Held("report", f"source.membership: {path}: retained function body missing")
    owned = {row.path for rows in current.units.values() for row in rows if row.kind == "c"}
    for path, (_, names, guarded) in current.sources.items():
        if names and not guarded and path not in owned:
            raise Held("report", f"source.membership: src/{path}.c: C definitions have no current C build owner")
    for name, receipt in current.receipts.items():
        source = current.sources.get(name)
        if source is None or not source[2]:
            raise Held("report", f"source.receipt: {name}: receipt requires guarded retained C")
        if source[0] != receipt["source_sha256"]:
            raise Held("report", f"source.hash: {name}: retained source differs from receipt")
        if receipt["compiler"] != project.compiler_reference(name):
            raise Held("report", f"source.compiler: {name}: compiler differs from receipt")
        if set(receipt["versions"]) != holdings.get(name, set()):
            raise Held("report", f"source.versions: {name}: receipt and current version membership differ")
        for version in receipt["versions"]:
            if name not in current.definitions[name, version]:
                raise Held("report", f"source.membership: {name} VERSION {version}: retained active C body missing")
            owners = [row for row in current.units[version] if any(name in m.aliases for m in split.unit_members(row))]
            if len(owners) != 1 or owners[0].kind != "asm":
                raise Held("report", f"source.membership: {name} VERSION {version}: draft must have one asm owner")
