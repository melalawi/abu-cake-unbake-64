"""Explicit unresolved compiler sets and evidence-backed regional pins."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import toml  # type: ignore[import-untyped]

from unbake.project import toolchain
from unbake.project.config import Held, Project


def read(value: object, compilers: dict[str, Any]) -> dict[str, tuple[str, ...]]:
    if not isinstance(value, dict):
        raise Held("config", "compiler.tied_set: expected table")
    result = {}
    for ref, ids in value.items():
        if (
            not isinstance(ref, str)
            or not ref.startswith("tie:")
            or len(ref) <= 4
            or not isinstance(ids, list)
            or len(ids) < 2
            or any(not isinstance(ident, str) or ident not in compilers for ident in ids)
            or len(set(ids)) != len(ids)
        ):
            raise Held("config", f"compiler.tied_set: {ref}: expected at least two distinct configured compiler IDs")
        result[ref] = tuple(sorted(ids))
    return result


def reference(project: Project, source: Path, *, equivalent: bool = False) -> str | None:
    ident = project.compiler_reference(source)
    if ident in project.compiler_ties:
        return ident
    if equivalent:
        data = toml.loads((project.root / "config.toml").read_text())
        for ref, selection in data.get("compiler_selections", {}).items():
            if selection.get("status") == "equivalent" and selection.get("function") == source.stem:
                if ref not in project.compiler_ties:
                    raise Held("try", f"compiler.tied_set: {ref}: equivalent candidates missing")
                return str(ref)
    return None


def equivalent_choice(project: Project, source: Path, ids: list[str]) -> tuple[str, str]:
    units = {
        name: value
        for name, value in project.units.items()
        if name not in (source.stem, (project.src.relative_to(project.root) / source.name).as_posix())
    }
    region = replace(project, units=units).compiler_reference(source)
    if region in ids:
        return region, "region's decided compiler is in the exact candidate set"
    proposal_path = project.root / "docs/setup/compiler.json"
    if proposal_path.is_file():
        proposal = json.loads(proposal_path.read_bytes())
        regions = proposal.get("candidate_rules", {}).get("tie:unit:" + source.stem, {}).get("regions", [])
        decided = {proposal.get("region_choices", {}).get(name) for name in regions} - {None}
        if len(decided) == 1 and (decided_region := next(iter(decided))) in ids:
            return decided_region, "region's decided compiler is in the exact candidate set"
    return next(ident for ident in toolchain.registry() if ident in ids), "first exact member in registry order"


def candidate(project: Project, ref: str, ident: str, function: str | None = None) -> Project:
    if ident not in project.compiler_ties[ref]:
        raise Held("try", f"compiler.tied_set: {ref}: {ident} is not a member")
    units = {name: ident if value == ref else value for name, value in project.units.items()}
    if function is not None:
        units[function] = ident
        units.pop((project.src.relative_to(project.root) / (function + ".c")).as_posix(), None)
    return replace(
        project,
        units=units,
        default_compiler=ident if project.default_compiler == ref else project.default_compiler,
    )


def selected(project: Project, evidence: dict[str, Any]) -> Project:
    """Apply one measured decision in memory, never to neighbouring items."""
    function, ident = evidence["function"], evidence["selected"]
    units = dict(project.units)
    units[function] = ident
    units.pop((project.src.relative_to(project.root) / (function + ".c")).as_posix(), None)
    return replace(project, units=units)


def publication_evidence(project: Project, evidence: dict[str, Any]) -> dict[str, Any]:
    """Emit structured measurements; diagnostics belong in trial logs."""

    def structured(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                key: structured(item)
                for key, item in value.items()
                if key not in {"differences", "error", "config_sha256"}
            }
        if isinstance(value, list):
            return [structured(item) for item in value]
        return value

    result: dict[str, Any] = structured(evidence)
    result["generations"] = {
        version: (project.build.relative_to(project.root) / Path(generation).name).as_posix()
        for version, generation in evidence.get("generations", {}).items()
    }
    return result


def fold(project: Project, receipts: list[dict[str, Any]]) -> dict[str, Any]:
    """Fold only the requested sources' measurements into staged configuration."""
    data = toml.loads((project.root / "config.toml").read_text())
    for selection in data.get("compiler_selections", {}).values():
        selection["evidence_json"] = json.dumps(
            publication_evidence(project, json.loads(selection["evidence_json"])), sort_keys=True
        )
    for evidence in receipts:
        evidence = publication_evidence(project, evidence)
        function, ident = evidence["function"], evidence["selected"]
        ref = "tie:unit:" + function
        data.setdefault("units", {})[function] = ident
        data["units"].pop((project.src.relative_to(project.root) / (function + ".c")).as_posix(), None)
        selection = {"compiler": ident, "function": function, "evidence_json": json.dumps(evidence, sort_keys=True)}
        if evidence["reason"] == "equivalent":
            data.setdefault("compiler_ties", {})[ref] = evidence["exact_candidates"]
            selection.update(
                status="equivalent", candidates=evidence["exact_candidates"], build_rule=evidence["build_rule"]
            )
        data.setdefault("compiler_selections", {})[ref] = selection
    return cast(dict[str, Any], data)
