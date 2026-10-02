"""Review and prove symbol correspondence on retained executable boundaries."""

from __future__ import annotations

import json
import re
import tempfile
from collections import defaultdict
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import toml  # type: ignore[import-untyped]

from unbake.cli.common import suggest
from unbake.layout import planner, port, split
from unbake.layout.symbol_identity import similarity_distribution
from unbake.project import setup, toolchain
from unbake.project.config import Held, Project, SetupPolicy, SymbolPolicy
from unbake.project.flow import FunctionRecord, LayoutManifest
from unbake.project.rom import load


def plan(project: Project, policy: SetupPolicy) -> tuple[dict[str, str], dict[str, Any]]:
    """Only instruction/position/graph evidence establishes identity."""
    ff = {v: port.functions(project, v) for v in project.versions}
    images = {v: load(project.version(v).baserom, retain_data=False) for v in project.versions}
    layout_path = project.root / "docs/setup/layout.json"
    try:
        layout = cast(LayoutManifest, json.loads(layout_path.read_bytes()))
    except (OSError, ValueError) as error:
        raise Held("setup", f"setup.symbol_layout: {error}") from error
    pins = {v: project.version(v).baserom_sha1 for v in project.versions}
    if (
        {v: image.sha1 for v, image in images.items()} != pins
        or layout.get("rom_sha1") != pins
        or layout.get("project_id") != project.id
        or layout.get("workspace_id") != project.workspace_id
        or set(layout.get("versions", {})) != set(project.versions)
    ):
        raise Held("setup", "setup.symbol_layout_stale: ROM pins, project identity or layout versions differ")
    evidence: dict[str, dict[int, str]] = {}
    details: dict[str, dict[int, dict[str, Any]]] = {}
    names = planner.correspondence(
        images,
        ff,
        project.names_from,
        symbol_policy=SymbolPolicy(policy.symbol_similarity_threshold, policy.symbol_similarity_margin),
        evidence=evidence,
        symbol_evidence=details,
        preserve_names=True,
        loaded_spans={v: row["loaded_spans"] for v, row in layout["versions"].items()},
    )
    destinations: dict[str, set[str]] = defaultdict(set)
    placements = []
    for v, rows in ff.items():
        for f in rows:
            name = names[v][f.start]
            destinations[f.name].add(name)
            if name != f.name:
                placements.append(
                    dict(
                        version=v,
                        start=f.start,
                        end=f.end,
                        address=f.address,
                        old=f.name,
                        new=name,
                        reason=evidence[v][f.start],
                    )
                )
    conflicts = [old for old, new in destinations.items() if len(new) != 1]
    if conflicts:
        raise Held(
            "setup",
            "setup.symbol_existing_conflict: existing symbols have inconsistent proved placements: "
            + ", ".join(sorted(conflicts)),
        )
    replacements = {old: next(iter(new)) for old, new in destinations.items() if old not in new}
    for v, rows in ff.items():
        old_records = {f["start"]: f for f in layout["versions"][v]["functions"]}
        image = images[v].image()
        records = []
        for f in rows:
            body = planner.body_identity(image, f.start, f.end)
            record = FunctionRecord(
                start=f.start,
                end=f.end,
                address=f.address,
                name=names[v][f.start],
                body_sha256=body["body_sha256"],
                normalized_body_sha256=body["normalized_body_sha256"],
                evidence={
                    **(old_records[f.start]["evidence"] if f.start in old_records else {}),
                    "correspondence": evidence[v][f.start],
                    "symbol_correspondence": details[v][f.start],
                    "assembly": f.kind == "asm",
                    "compiler_reference": project.compiler_reference(f.path),
                },
            )
            records.append(record)
        layout["versions"][v]["functions"] = records
        del image
    layout["items"] = planner.symbol_items(layout["versions"])
    for item in layout["items"].values():
        for record in item["placements"].values():
            record["evidence"]["holding_versions"] = item["versions"]
            record["evidence"]["name_source"] = item["versions"][0]
    layout["inputs_sha256"]["symbol_replan"] = planner.digest([placements, layout["items"]])
    report = {
        "schema": 1,
        "symbol_policy": {"threshold": policy.symbol_similarity_threshold, "margin": policy.symbol_similarity_margin},
        "similarity_distribution": similarity_distribution(details),
        "placements": placements,
        "replacements": replacements,
        "old_items": len(destinations),
        "new_items": len(layout["items"]),
        "layout": layout,
    }
    return replacements, report


def rewrite(text: str, replacements: dict[str, str]) -> str:
    # Identifiers only: retain strings, comments and formatting verbatim.
    return re.sub(
        r'/\*.*?\*/|//[^\n]*|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|(?P<id>\b[A-Za-z_]\w*\b)',
        lambda m: replacements.get(m["id"], m[0]) if m["id"] else m[0],
        text,
        flags=re.S,
    )


def path_name(name: str, replacements: dict[str, str]) -> str:
    return "/".join(replacements.get(part, part) for part in name.split("/"))


def rewrite_layout(text: str, replacements: dict[str, str]) -> str:
    lines = []
    for line in text.splitlines(keepends=True):
        row = split.ROW.fullmatch(line)
        if row:
            name = split.plain(row["path"])
            line = split.replace_row(line, row, path=path_name(name, replacements))
        lines.append(line)
    return "".join(lines)


def run(project: Project, policy: SetupPolicy, confirm: str | None) -> list[str]:
    replacements, report = plan(project, policy)
    inputs = setup._inputs(project)
    token = planner.digest([inputs, report])
    directory = project.build / "setup"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "symbol-proposal.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(
        f"OK(setup): symbol items {report['old_items']} -> {report['new_items']}; "
        f"{len(report['placements'])} placement names change"
    )
    if confirm is None:
        print(f"OK(setup): review build/setup/symbol-proposal.json; setup --replan-symbols --confirm {token}")
        suggest(f"unbake setup --replan-symbols --confirm {token}")
        return ["symbol proposal ready; executable boundaries retained"]
    if confirm != token:
        raise Held("setup", "setup.symbol_proposal_stale: symbol proposal or project inputs changed")
    if not replacements:
        return ["symbol names unchanged"]
    for source in project.src.rglob("*.c"):
        text = source.read_text()
        if rewrite(text, replacements) != text or source.stem in replacements:
            raise Held(
                "setup",
                f"setup.symbol_authored_source: {source.relative_to(project.root)}: "
                "changed identities require reviewed C before replanning",
            )
    data = toml.loads((project.root / "config.toml").read_text())
    groups: dict[str, set[str]] = defaultdict(set)
    for v in project.versions:
        for f in port.functions(project, v):
            groups[replacements.get(f.name, f.name)].update(
                project.compiler_ties.get(project.compiler_reference(f.path), (project.compiler_reference(f.path),))
            )
    affected = set(replacements) | set(replacements.values())
    for old in affected:
        data["units"].pop(old, None)
    registry = list(toolchain.registry())
    for name in set(replacements.values()):
        candidates = sorted(groups[name], key=registry.index)
        if len(candidates) == 1:
            data["units"][name] = candidates[0]
        else:
            reference = "tie:symbol:" + name
            data.setdefault("compiler_ties", {})[reference] = candidates
            data["units"][name] = reference
    for key, selection in list(data.get("compiler_selections", {}).items()):
        measured = json.loads(selection.get("evidence_json", "{}"))
        if selection.get("function", measured.get("function")) in affected:
            report.setdefault("prior_compiler_selections", {})[key] = selection
            del data["compiler_selections"][key]
    with tempfile.TemporaryDirectory(prefix="symbol-proof-", dir=directory) as temporary:
        tree = Path(temporary) / "tree"
        setup._copy_inputs(project, tree, inputs)
        for v in project.versions:
            target = tree / project.version(v).split.relative_to(project.root)
            target.write_text(rewrite_layout(target.read_text(), replacements))
            target = tree / project.version(v).symbols.relative_to(project.root)
            target.write_text(rewrite(target.read_text(), replacements))
        for header in (tree / path.relative_to(project.root) for path in project.include):
            for path in header.rglob("*.h"):
                path.write_text(rewrite(path.read_text(), replacements))
        for version in report["layout"]["versions"].values():
            if "split_yaml" in version["evidence"]:
                version["evidence"]["split_yaml"] = rewrite_layout(version["evidence"]["split_yaml"], replacements)
            if "symbols_text" in version["evidence"]:
                version["evidence"]["symbols_text"] = rewrite(version["evidence"]["symbols_text"], replacements)
            for provider in version["providers"]:
                provider["name"] = path_name(provider["name"], replacements)
                provider["owners"] = [replacements.get(owner, owner) for owner in provider["owners"]]
        (tree / "config.toml").write_text(toml.dumps(data))
        (tree / "docs/setup/layout.json").write_text(json.dumps(report["layout"], indent=2, sort_keys=True) + "\n")
        (tree / "docs/setup/symbol-correspondence.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
        del report, data, groups
        # Symbol updates rebuild every object. Prove versions in turn so
        # simultaneous large linkers do not multiply resident cartridge work.
        # The per-version build still uses the configured core budget.
        proof_policy = replace(policy, setup_version_jobs=1)
        return setup._prove_publish(project, tree, proof_policy, inputs, fresh=True, supply=None)
