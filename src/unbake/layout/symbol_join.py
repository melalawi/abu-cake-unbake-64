"""Byte-pinned user correspondence assertions, proved as one symbol transaction."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from itertools import combinations
from pathlib import Path
from typing import Any, cast

from unbake.layout import planner, port, split, symbol_identity, symbol_replan
from unbake.project import setup
from unbake.project.config import Held, Project, SetupPolicy
from unbake.project.flow import LayoutManifest
from unbake.project.rom import load


def read(path: Path) -> list[dict[str, Any]]:
    def pairs(values: list[tuple[str, Any]]) -> dict[str, Any]:
        result = {}
        for key, value in values:
            if key in result:
                raise Held("split", f"split.join.map: duplicate key {key}")
            result[key] = value
        return result

    try:
        value = json.loads(path.read_bytes(), object_pairs_hook=pairs)
    except (OSError, ValueError) as error:
        raise Held("split", f"split.join.map: {error}") from error
    if not isinstance(value, list) or not value or any(not isinstance(row, dict) for row in value):
        raise Held("split", "split.join.map: required nonempty JSON list of name/placements/evidence objects")
    return cast(list[dict[str, Any]], value)


def plan(project: Project, assertions: list[dict[str, Any]]) -> tuple[dict[str, str], dict[str, Any]]:
    """Validate all requests together; any refusal prevents the entire batch."""
    try:
        layout = cast(LayoutManifest, json.loads((project.root / "docs/setup/layout.json").read_bytes()))
    except (OSError, ValueError) as error:
        raise Held("split", f"split.join.layout: {error}") from error
    ff = {v: port.functions(project, v) for v in project.versions}
    functions = {(v, f.start): f for v, rows in ff.items() for f in rows}
    records = {(v, f["start"]): f for v, row in layout["versions"].items() for f in row["functions"]}
    images = {v: load(project.version(v).baserom, retain_data=False) for v in project.versions}
    pins = {v: project.version(v).baserom_sha1 for v in project.versions}
    if (
        layout.get("project_id") != project.id
        or layout.get("workspace_id") != project.workspace_id
        or layout.get("rom_sha1") != pins
        or {v: image.sha1 for v, image in images.items()} != pins
        or set(functions) != set(records)
        or any(
            (f.end, f.address, f.name) != (records[k]["end"], records[k]["address"], records[k]["name"])
            for k, f in functions.items()
        )
    ):
        raise Held("split", "split.join.layout_stale: ROM pins, project identity or executable placements changed")
    items: dict[str, set[tuple[str, int]]] = defaultdict(set)
    for key, f in functions.items():
        items[f.name].add(key)
    groups: dict[str, set[tuple[str, int]]] = {}
    proofs = {}
    refusals = []
    used: set[tuple[str, int]] = set()
    for request in assertions:
        name = request.get("name", "<unnamed>")
        try:
            split.name(name, "split.join.name")
            placements = request.get("placements")
            if not isinstance(placements, list) or len(placements) < 2:
                raise Held("split", "split.join.placements: supply at least two byte-pinned placements")
            if not isinstance(request.get("evidence"), (str, dict)) or not request["evidence"]:
                raise Held("split", "split.join.evidence: supply correspondence evidence")
            keys = set()
            for placement in placements:
                if not isinstance(placement, dict):
                    raise Held("split", "split.join.placements: expected placement object")
                v, start, end = (placement.get(field) for field in ("version", "start", "end"))
                if not isinstance(v, str) or type(start) is not int or type(end) is not int:
                    raise Held("split", "split.join.placements: version string and integer start/end required")
                key = (v, start)
                if key in keys:
                    raise Held("split", "split.join.duplicate_version: repeated placement")
                requested_function = functions.get(key)
                if requested_function is None or requested_function.end != end:
                    raise Held("split", f"split.join.placement_stale: {v}:{start:#x}: executable boundary differs")
                image = images[v].image()
                digest = hashlib.sha256(image[start:end]).hexdigest()
                del image
                if placement.get("body_sha256") != digest:
                    raise Held("split", f"split.join.placement_stale: {v}:{start:#x}: body SHA256 differs")
                keys.add(key)
            # An assertion joins whole existing items; it never detaches one
            # placement from its already established symbol correspondence.
            members = set().union(*(items[functions[key].name] for key in keys))
            if len({v for v, _ in members}) != len(members):
                raise Held("split", "split.join.duplicate_version: existing items overlap in a version")
            if name in groups or members & used:
                raise Held("split", "split.join.overlap: batch repeats a name or an existing item")
            source_names = {functions[key].name for key in members}
            for v in project.versions:
                if name in split.symbols(project.version(v).symbols)[1] and name not in source_names:
                    raise Held("split", f"split.join.name_conflict: {v}: symbol {name} already exists")
            groups[name] = members
            used.update(members)
            proofs[name] = request["evidence"]
        except Held as error:
            refusals.append({"name": name, "reason": error.reason})
    replacements = {
        functions[k].name: name for name, members in groups.items() for k in members if functions[k].name != name
    }
    final = {k: replacements.get(f.name, f.name) for k, f in functions.items()}
    final_items: dict[str, dict[str, int]] = defaultdict(dict)
    for (v, start), name in final.items():
        final_items[name][v] = start
    outgoing, incoming, unresolved = symbol_identity.graph(
        images, ff, loaded_spans={v: row["loaded_spans"] for v, row in layout["versions"].items()}
    )
    accepted = []
    for name, members in groups.items():
        checks = []
        reasons = []
        for a, b in combinations(sorted(members), 2):
            av, bv = a[0], b[0]
            # Existing body identities can occur in globally reordered spans.
            # Use each placement's immediate shared bounding anchors, as the
            # automatic correspondence boundary does, in both directions.
            anchors = {
                anchor: positions
                for anchor, positions in final_items.items()
                if anchor != name and av in positions and bv in positions
            }
            bounds = set()
            for key in (a, b):
                before = [anchor for anchor, positions in anchors.items() if positions[key[0]] < key[1]]
                after = [anchor for anchor, positions in anchors.items() if positions[key[0]] > key[1]]
                if before:
                    bounds.add(max(before, key=lambda anchor: anchors[anchor][key[0]]))
                if after:
                    bounds.add(min(after, key=lambda anchor: anchors[anchor][key[0]]))
            for anchor in sorted(bounds):
                positions = anchors[anchor]
                if (positions[av] < a[1]) != (positions[bv] < b[1]):
                    reasons.append(f"split.join.anchor_order: {av}/{bv}: crosses enclosing anchor {anchor}")
            checks.append({"positions": [list(a), list(b)], "enclosing_anchors": sorted(bounds)})
            common = {symbol for symbol, positions in final_items.items() if av in positions and bv in positions}
            profiles = []
            for key in (a, b):
                profiles.append(
                    {
                        kind: sorted({final[peer] for peer in edges[key]} & common)
                        for kind, edges in (("callers", incoming), ("callees", outgoing))
                    }
                )
            checks.append({"positions": [list(a), list(b)], "common_graph": profiles})
            if profiles[0] != profiles[1]:
                reasons.append(f"split.join.graph_contradiction: {av}/{bv}: proven shared caller/callee edges differ")
        for reason in sorted(set(reasons)):
            refusals.append({"name": name, "reason": reason})
        placements = []
        for key in sorted(members):
            f = functions[key]
            image = images[key[0]].image()
            body = planner.body_identity(image, f.start, f.end)
            del image
            placements.append(dict(version=key[0], start=f.start, end=f.end, body_sha256=body["body_sha256"]))
            records[key]["name"] = name
            records[key]["body_sha256"] = body["body_sha256"]
            records[key]["normalized_body_sha256"] = body["normalized_body_sha256"]
            records[key]["evidence"].update(correspondence="user-assertion", symbol_assertion=name)
        accepted.append(
            {
                "name": name,
                "placements": placements,
                "evidence": proofs[name],
                "validation": checks,
                "unknown_transfers": {
                    f"{v}:{start}": unresolved[v, start] for v, start in sorted(members) if (v, start) in unresolved
                },
            }
        )
    previous = [
        row
        for row in layout.get("symbol_assertions", [])
        if not any((p["version"], p["start"]) in used for p in row["placements"])
    ]
    layout["symbol_assertions"] = previous + accepted
    layout["versions"] = {v: layout["versions"][v] for v in project.versions}
    layout["items"] = planner.symbol_items(layout["versions"])
    for item in layout["items"].values():
        for record in item["placements"].values():
            record["evidence"]["holding_versions"] = item["versions"]
            record["evidence"]["name_source"] = item["versions"][0]
    layout["inputs_sha256"]["symbol_join"] = planner.digest(layout["symbol_assertions"])
    placements_changed = [
        {
            "version": v,
            "start": at,
            "end": functions[v, at].end,
            "address": functions[v, at].address,
            "old": functions[v, at].name,
            "new": name,
            "reason": "user-assertion",
        }
        for (v, at), name in final.items()
        if name != functions[v, at].name
    ]
    report = {
        "schema": 1,
        "assertions": accepted,
        "refusals": refusals,
        "replacements": replacements,
        "placements": placements_changed,
        "old_items": len(items),
        "new_items": len(layout["items"]),
        "layout": layout,
    }
    return replacements, report


def run(project: Project, policy: SetupPolicy, path: Path, *, apply: bool) -> list[str]:
    inputs = setup._inputs(project)
    replacements, report = plan(project, read(path))
    if setup._inputs(project) != inputs:
        raise Held("split", "split.join.stale: project inputs changed during validation")
    directory = project.build / "setup"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "join-proposal.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    if report["refusals"]:
        raise Held("split", "; ".join(f"{r['name']}: {r['reason']}" for r in report["refusals"]))
    if not apply:
        return [
            f"preview {len(report['assertions'])} joins; items {report['old_items']} -> {report['new_items']}; "
            "review build/setup/join-proposal.json; apply with split join --map FILE --apply"
        ]
    for source in project.src.rglob("*.c"):
        if source.stem in replacements or symbol_replan.rewrite(source.read_text(), replacements) != source.read_text():
            raise Held("split", f"split.join.authored_source: {source.relative_to(project.root)}: affected authored C")
    return symbol_replan.publish(project, policy, replacements, report, inputs)
