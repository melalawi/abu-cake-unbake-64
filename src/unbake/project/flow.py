"""Frozen setup and work interchange contracts, independent of implementations."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal, NotRequired, TypedDict

from unbake.project.census import Census
from unbake.project.config import PendingProject, SetupPolicy


class Identity(TypedDict):
    schema: int
    project_id: str
    workspace_id: str
    rom_sha1: dict[str, str]


class Span(TypedDict):
    start: int
    end: int
    address: int | None


class FunctionRecord(Span):
    name: str
    evidence: dict[str, Any]


class CrossVersionItem(TypedDict):
    name: str
    versions: list[str]
    placements: dict[str, FunctionRecord]
    evidence: dict[str, Any]


class ProviderRecord(Span):
    name: str
    kind: Literal["private", "shared", "writable", "unresolved", "text", "bin"]
    owners: list[str]
    evidence: dict[str, Any]


class VersionLayout(TypedDict):
    functions: list[FunctionRecord]
    providers: list[ProviderRecord]
    loaded_spans: list[Span]
    evidence: dict[str, Any]


class LayoutManifest(Identity):
    names_from: str
    versions: dict[str, VersionLayout]
    inputs_sha256: dict[str, str]


class CompilerCandidate(TypedDict):
    id: str
    available: bool
    rank: list[int]
    evidence: dict[str, Any]


class CompilerProposal(Identity):
    compiler_ties: NotRequired[dict[str, list[str]]]
    choices: NotRequired[dict[str, str]]
    layout_sha256: str
    inputs_sha256: dict[str, str]
    default_compiler: str | None
    assignments: dict[str, str]
    cflags: dict[str, list[str]]
    candidates: dict[str, list[CompilerCandidate]]
    unresolved: list[str]


class WorkManifest(Identity):
    entries: NotRequired[dict[str, dict[str, object]]]
    kind: Literal["function", "struct"]
    subject: str
    source: str
    source_sha256: str
    overlay_sha256: str
    names_from: str
    versions: list[str]
    compiler_sha256: dict[str, str]
    flags: dict[str, list[str]]
    target_sha256: dict[str, str]
    layout_sha256: str
    needs: dict[str, Any]
    evidence: dict[str, Any]
    compiler_evidence: dict[str, Any]


def plan_layout(project: PendingProject, census: Census, policy: SetupPolicy) -> LayoutManifest:
    """Build the pure joint plan; implementation supplied by the layout owner."""
    from unbake.layout.planner import plan_layout as implementation

    return implementation(project, census, policy)


def propose_compilers(
    project: PendingProject,
    census: Census,
    layout: LayoutManifest,
    policy: SetupPolicy,
    *,
    choices: dict[str, str] | None = None,
) -> CompilerProposal:
    """Rank candidates without confirming or publishing compiler assignments."""
    from unbake.project.fingerprint import propose_compilers as implementation

    return implementation(project, census, layout, policy, choices=choices)


def complete_setup(
    project: PendingProject,
    census: Census,
    layout: LayoutManifest,
    proposal: CompilerProposal,
    policy: SetupPolicy,
    *,
    confirm: str | None = None,
    supply: Path | None = None,
) -> list[str]:
    """Confirm, stage, prove all versions, then publish readiness atomically."""
    from unbake.project.setup import complete_setup as implementation

    return implementation(project, census, layout, proposal, policy, confirm=confirm, supply=supply)
