"""Byte-pinned user correspondence assertions, proved as one symbol transaction."""

from __future__ import annotations

import hashlib
import json
import re
import shlex
import uuid
from collections import defaultdict
from dataclasses import replace
from itertools import combinations
from pathlib import Path
from typing import Any, cast

from unbake.cli.common import suggest
from unbake.layout import planner, port, split, symbol_identity, symbol_replan
from unbake.project import config, setup
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
    """Validate a simultaneous batch and retain only its independently passing subset."""
    try:
        with (project.build / "setup/layout.json").open() as stream:
            layout = cast(LayoutManifest, json.load(stream))
    except (OSError, ValueError) as error:
        raise Held("split", f"split.join.layout: {error}") from error
    ff = {v: port.functions(project, v) for v in project.versions}
    functions = {(v, f.start): f for v, rows in ff.items() for f in rows}
    records = {(v, f["start"]): f for v, row in layout["versions"].items() for f in row["functions"]}
    images = {v: load(project.version(v).baserom, retain_data=True) for v in project.versions}
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
    candidates: list[tuple[str, set[tuple[str, int]], Any]] = []
    proofs = {}
    refusals = []
    used: set[tuple[str, int]] = set()
    symbol_names = {v: split.symbols(project.version(v).symbols)[1] for v in project.versions}
    authored = {source.stem: source for source in project.src.rglob("*.c")}
    pending_sources = set(authored) - {f.name for f in functions.values() if f.kind == "c"}
    data_assertions = []
    for request in assertions:
        if request.get("kind") == "data":
            data_assertions.append(request)
            continue
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
            source_names = {functions[key].name for key in members}
            if pending := source_names & pending_sources:
                raise Held(
                    "split", "split.join.authored_source: unpublished C requires review: " + ", ".join(sorted(pending))
                )
            if name in authored and name not in source_names:
                raise Held("split", f"split.join.authored_source: destination {name}.c already exists")
            sources = [authored[old] for old in sorted(source_names & authored.keys())]
            if len(sources) > 1:
                proposed = {old: name for old in source_names}
                if len({symbol_replan.rewrite(source.read_text(), proposed) for source in sources}) != 1:
                    raise Held(
                        "split",
                        "split.join.authored_source: joined C sources differ; review "
                        + ", ".join(p.name for p in sources),
                    )
            for v in project.versions:
                if name in symbol_names[v] and name not in source_names:
                    raise Held("split", f"split.join.name_conflict: {v}: symbol {name} already exists")
            candidates.append((name, members, request["evidence"]))
        except Held as error:
            refusals.append({"name": name, "reason": error.reason})
    if any(len({functions[key].name for key in members}) > 1 for _, members, _ in candidates):
        outgoing, incoming, unresolved = symbol_identity.graph(
            images, ff, loaded_spans={v: row["loaded_spans"] for v, row in layout["versions"].items()}
        )
    else:
        outgoing, incoming, unresolved = {}, {}, {}
    while True:
        groups = {}
        used = set()
        overlaps = []
        selected = {}
        for index, (name, members, proof) in enumerate(candidates):
            if name in groups or members & used:
                overlaps.append(
                    {"name": name, "reason": "split.join.overlap: batch repeats a name or an existing item"}
                )
            else:
                groups[name] = members
                used.update(members)
                proofs[name] = proof
                selected[name] = index
        replacements = {
            functions[k].name: name for name, members in groups.items() for k in members if functions[k].name != name
        }
        final = {k: replacements.get(f.name, f.name) for k, f in functions.items()}
        final_items: dict[str, dict[str, int]] = defaultdict(dict)
        for (v, start), name in final.items():
            final_items[name][v] = start
        validations: dict[str, list[dict[str, Any]]] = {}
        rejected = set()
        for name, members in groups.items():
            checks: list[dict[str, Any]] = []
            reasons = []
            existing = {functions[key].name for key in members}
            if len(existing) == 1:
                # Reasserting or renaming a retained whole item introduces no
                # new correspondence. New neighbouring identities cannot
                # invalidate its already pinned placements on a name-only edit.
                validations[name] = [{"retained_item": next(iter(existing)), "identity": "unchanged placements"}]
                continue
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
                        reasons.append(
                            f"split.join.anchor_order: {av}:{a[1]:#x}/{bv}:{b[1]:#x}: "
                            f"crosses enclosing anchor {anchor} at {positions[av]:#x}/{positions[bv]:#x}; "
                            "placements fall on opposite sides; review the pinned boundaries"
                        )
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
                    reasons.append(
                        f"split.join.graph_contradiction: {av}:{a[1]:#x}/{bv}:{b[1]:#x}: "
                        f"resolved shared edges differ: {json.dumps(profiles, sort_keys=True)}; "
                        "review these targets and version-specific control flow"
                    )
            for reason in sorted(set(reasons)):
                refusals.append({"name": name, "reason": reason})
            if reasons:
                rejected.add(name)
            validations[name] = checks
        if not rejected:
            refusals.extend(overlaps)
            break
        excluded = {selected[name] for name in rejected}
        candidates = [row for index, row in enumerate(candidates) if index not in excluded]
    accepted = []
    prior_transfers = {
        (p["version"], p["start"]): row.get("unknown_transfers", {}).get(f"{p['version']}:{p['start']}")
        for row in layout.get("symbol_assertions", [])
        for p in row["placements"]
    }
    used = set().union(*groups.values()) if groups else set()
    for name, members in groups.items():
        checks = validations[name]
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
                    f"{v}:{start}": unresolved.get((v, start), prior_transfers.get((v, start)))
                    for v, start in sorted(members)
                    if (v, start) in unresolved or prior_transfers.get((v, start)) is not None
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
    data_tables = symbol_replan.project_data_tables(project)
    # Discover generated D labels through the same relocation boundary before
    # checking assertions; they need not already be explicit symbol declarations.
    named_functions = {v: [replace(f, name=final[v, f.start]) for f in rows] for v, rows in ff.items()}
    symbol_identity.data_identity(images, named_functions, data_tables, lambda f: port.object_path(project, f))
    validated_data: list[dict[str, Any]] = []
    for request in data_assertions:
        name = request.get("name", "<unnamed>")
        try:
            split.name(name, "split.join.name")
            if not request.get("evidence") or not isinstance(request["evidence"], (str, dict)):
                raise Held("split", "split.join.evidence: supply correspondence evidence")
            if any(function.name == name for function in functions.values()):
                raise Held("split", f"split.join.name_conflict: function symbol {name} already exists")
            rows = request.get("placements")
            if not isinstance(rows, list) or len(rows) < 2:
                raise Held("split", "split.join.placements: supply at least two ROM-pinned data placements")
            versions = set()
            for row in rows:
                if not isinstance(row, dict):
                    raise Held("split", "split.join.placements: expected placement object")
                v, symbol = row.get("version"), row.get("symbol")
                if not isinstance(v, str) or v not in data_tables or not isinstance(symbol, str):
                    raise Held("split", "split.join.data_placement_stale: unknown version or data symbol")
                entry = data_tables[v].get(symbol)
                if entry is None and type(row.get("address")) is int:
                    encoded = re.fullmatch(r"D_([0-9A-Fa-f]{8})", symbol)
                    if encoded and int(encoded[1], 16) == row["address"]:
                        entry = symbol_identity.DataSymbol(row["address"])
                        data_tables[v][symbol] = entry
                if entry is None or type(row.get("address")) is not int or entry.address != row["address"]:
                    raise Held("split", "split.join.data_placement_stale: data address differs")
                if row.get("rom_sha1") != pins[v]:
                    raise Held("split", "split.join.data_placement_stale: ROM SHA1 differs")
                if v in versions:
                    raise Held("split", "split.join.duplicate_version: repeated data version")
                versions.add(v)
                if row.get("size", entry.size) != entry.size:
                    raise Held("split", "data.size_conflict: declared size differs")
                if row.get("type", entry.kind) != entry.kind:
                    raise Held("split", "data.kind_conflict: declared kind differs")
            if name in groups or any(name == old["name"] for old in validated_data):
                raise Held("split", "split.join.overlap: repeated asserted name")
            validated_data.append(request)
        except Held as error:
            refusals.append({"name": name, "reason": error.reason})
    existing_data = layout.get("data_assertions", [])
    data = symbol_identity.data_identity(
        images,
        named_functions,
        data_tables,
        lambda f: port.object_path(project, f),
        existing_data + validated_data,
        symbol_replan.header_data_types(project),
    )
    requested = {(p["version"], p["symbol"]) for row in validated_data for p in row["placements"]}
    for refusal in data["refusals"]:
        if any(tuple(member) in requested for member in refusal["members"]):
            refusals.append({"name": "data", "reason": ", ".join(refusal["reasons"])})
    layout["data_assertions"] = existing_data + validated_data
    layout["data_symbols"] = data
    report = {
        "data_symbols": data,
        "schema": 1,
        "assertions": accepted + validated_data,
        "refusals": refusals,
        "replacements": replacements,
        "placements": placements_changed,
        "old_items": len(items),
        "new_items": len(layout["items"]),
        "layout": layout,
    }
    return replacements, report


def run(project: Project, policy: SetupPolicy, path: Path, *, apply: bool) -> list[str]:
    command = shlex.join(["unbake", "split", "join", "--map", str(path.resolve())])
    suggest(command if apply else command + " --apply", on_refusal=True)
    assertions = read(path)
    directory = project.build / "setup" / ("join-" + uuid.uuid4().hex)
    directory.mkdir(parents=True)
    # Optimistic validation stays outside the publication lock. Concurrent
    # names are re-read and re-proved; stale bytes are still refused by name.
    for _ in range(8):
        project = config.load(project.root)
        inputs = setup._inputs(project)
        try:
            replacements, report = plan(project, assertions)
            if setup._inputs(project) != inputs:
                continue
            report["publication"] = "preview" if not apply else "pending"
            report["exit_status"] = None if apply else int(bool(report["refusals"]))
            artifact = directory / "proposal.json"
            with artifact.open("w") as stream:
                json.dump({k: v for k, v in report.items() if k != "layout"}, stream, indent=2, sort_keys=True)
                stream.write("\n")
            lines = [f"join receipt {artifact}"]
            if apply and report["assertions"]:
                lines += symbol_replan.publish(project, policy, replacements, report, inputs)
            if apply:
                report["publication"] = (
                    ("partial" if report["refusals"] else "complete") if report["assertions"] else "refused"
                )
                report["exit_status"] = int(bool(report["refusals"]))
                report["proof"] = lines[1:]
                with artifact.open("w") as stream:
                    json.dump({k: v for k, v in report.items() if k != "layout"}, stream, indent=2, sort_keys=True)
                    stream.write("\n")
            lines += [
                f"{'published' if apply else 'preview'} {len(report['assertions'])} passing joins; "
                f"{len({r['name'] for r in report['refusals']})} refused requests; "
                f"items {report['old_items']} -> {report['new_items']}"
            ]
            lines += [f"HELD(split): {r['name']}: {r['reason']}" for r in report["refusals"]]
            return lines
        except Held as error:
            if setup._inputs(project) == inputs and (
                "project inputs changed" not in error.reason and "generations changed" not in error.reason
            ):
                raise
        finally:
            # Large layout evidence must be released before the next attempt.
            if "report" in locals():
                del report
    raise Held("split", "split.join.stale: project kept changing; retry the byte-pinned map")
