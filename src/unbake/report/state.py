"""Current source publication state, independent of historical attempt maxima."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from unbake import cdecl, inputs
from unbake.config import Held, Project
from unbake.fold import source_views
from unbake.layout import split
from unbake.process import capture
from unbake.process import named as cause_named
from unbake.report import data
from unbake.work import attempts


@dataclass
class Inventory:
    units: dict[str, list[split.Function]]
    receipts: dict[str, dict[str, Any]]
    sources: dict[str, tuple[str, set[str], bool]]
    definitions: dict[tuple[str, str], set[str]]
    input_pins: dict[Path, inputs.Signature | None]
    receipt_pin: str
    ledger_signature: inputs.Signature | None
    data_coverage: dict[str, data.Coverage] = field(default_factory=dict)
    data_pin: str = ""


def input_signatures(project: Project) -> dict[Path, inputs.Signature | None]:
    from unbake.report import verify

    paths = (*verify.source_paths(project), *(project.version(v).baserom for v in project.versions))
    return {path: inputs.signature(path) if path.is_file() else None for path in paths}


def assert_current(project: Project, current: Inventory) -> None:
    if input_signatures(project) != current.input_pins or (
        ledger_signature(project) != current.ledger_signature
        and (
            receipt_identity(project) != current.receipt_pin
            or data.identity(data.snapshots(project)) != current.data_pin
        )
    ):
        raise Held(
            cause_named(
                "source.changed",
                "source.changed: current-source validation inputs changed; regenerate inventory",
                owner="report.state",
                stage="report",
            )
        )


@attempts.with_ledger
def inventory(project: Project, *, receipts: dict[str, dict[str, Any]] | None = None) -> Inventory:
    """Read each version once, each retained source once, and receipts once."""
    pins = input_signatures(project)
    units = {version: split.functions(project, version) for version in project.versions}
    history = attempts.ledger(project).fuzzy_sources()
    sources = {}
    definitions = {}
    for path in sorted(project.src.rglob("*.c")):
        raw = path.read_bytes()
        text = raw.decode()
        guarded = attempts.guard_present(text)
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
                    raise Held(
                        capture(
                            error,
                            cause=cause_named(
                                "source.syntax",
                                f"source.syntax: {path} VERSION {version}: {error.reason}",
                                owner="report.state",
                                stage="report",
                            ),
                        )
                    ) from error
            definitions[unit, version] = parsed_views[clean]
            names.update(parsed_views[clean])
        sources[unit] = (hashlib.sha256(raw).hexdigest(), names, guarded)
    if receipts is None:
        # History proves its own source, not a later retained provider/consumer edit.
        # Unknown similarity describes retention only; it is never new native proof.
        holdings: dict[str, set[str]] = {}
        for version, rows in units.items():
            for row in rows:
                for member in split.unit_members(row):
                    for name in member.aliases:
                        holdings.setdefault(name, set()).add(version)
        receipts = {}
        for name, (digest, _, guarded) in sources.items():
            if not guarded:
                continue
            receipt = history.get(name)
            receipts[name] = (
                receipt
                if receipt is not None and receipt["source_sha256"] == digest
                else {
                    "source_sha256": digest,
                    "compiler": project.compiler_reference(name),
                    "score": None,
                    "versions": {v: None for v in sorted(holdings.get(name, set()))},
                }
            )
    records = data.snapshots(project)
    coverage = {version: data.coverage(project, version, sources, records[version]) for version in project.versions}
    result = Inventory(
        units,
        receipts,
        sources,
        definitions,
        pins,
        inputs.bytes_digest(attempts.encoded(history), algorithm="sha256"),
        ledger_signature(project),
        coverage,
        data.identity(records),
    )
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
                        cause_named(
                            "source.state",
                            f"source.state: VERSION {version} {row.path}: exact C source missing or guarded",
                            owner="report.state",
                            stage="report",
                        )
                    )
                for member in split.unit_members(row):
                    if not set(member.aliases) & current.definitions[row.path, version]:
                        raise Held(
                            cause_named(
                                "source.membership",
                                f"source.membership: VERSION {version} {member.name}: C body missing",
                                owner="report.state",
                                stage="report",
                            )
                        )
            for member in split.unit_members(row):
                for name in member.aliases:
                    holdings.setdefault(name, set()).add(version)
    for path, (_, names, guarded) in current.sources.items():
        if not guarded:
            continue
        if path not in current.receipts:
            raise Held(
                cause_named(
                    "source.receipt",
                    f"source.receipt: src/{path}.c: retained source has no receipt; reconcile verified prior evidence",
                    owner="report.state",
                    stage="report",
                ),
            )
        if path not in names:
            raise Held(
                cause_named(
                    "source.membership",
                    f"source.membership: {path}: retained function body missing",
                    owner="report.state",
                    stage="report",
                )
            )
    owned = {row.path for rows in current.units.values() for row in rows if row.kind == "c"}
    for path, (_, names, guarded) in current.sources.items():
        if names and not guarded and path not in owned:
            raise Held(
                cause_named(
                    "source.membership",
                    f"source.membership: src/{path}.c: C definitions have no current C build owner",
                    owner="report.state",
                    stage="report",
                )
            )
    for name, receipt in current.receipts.items():
        source = current.sources.get(name)
        if source is None or not source[2]:
            raise Held(
                cause_named(
                    "source.receipt",
                    f"source.receipt: {name}: receipt requires guarded retained C",
                    owner="report.state",
                    stage="report",
                )
            )
        if source[0] != receipt["source_sha256"]:
            raise Held(
                cause_named(
                    "source.hash",
                    f"source.hash: {name}: retained source differs from receipt",
                    owner="report.state",
                    stage="report",
                )
            )
        if receipt["compiler"] != project.compiler_reference(name):
            raise Held(
                cause_named(
                    "source.compiler",
                    f"source.compiler: {name}: compiler differs from receipt",
                    owner="report.state",
                    stage="report",
                )
            )
        if set(receipt["versions"]) != holdings.get(name, set()):
            raise Held(
                cause_named(
                    "source.versions",
                    f"source.versions: {name}: receipt and current version membership differ",
                    owner="report.state",
                    stage="report",
                )
            )
        for version in receipt["versions"]:
            if name not in current.definitions[name, version]:
                raise Held(
                    cause_named(
                        "source.membership",
                        f"source.membership: {name} VERSION {version}: retained active C body missing",
                        owner="report.state",
                        stage="report",
                    )
                )
            owners = [row for row in current.units[version] if any(name in m.aliases for m in split.unit_members(row))]
            if len(owners) != 1 or owners[0].kind != "asm":
                raise Held(
                    cause_named(
                        "source.membership",
                        f"source.membership: {name} VERSION {version}: draft must have one asm owner",
                        owner="report.state",
                        stage="report",
                    )
                )


def receipt_identity(project: Project) -> str:
    return inputs.bytes_digest(attempts.encoded(attempts.ledger(project).fuzzy_sources()), algorithm="sha256")


def ledger_signature(project: Project) -> inputs.Signature | None:
    path = project.root / attempts.PATH
    return inputs.signature(path) if path.is_file() else None
