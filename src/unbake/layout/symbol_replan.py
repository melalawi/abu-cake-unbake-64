"""Review and prove symbol correspondence on retained executable boundaries."""

from __future__ import annotations

import csv
import json
import re
import tempfile
from collections import defaultdict
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import toml  # type: ignore[import-untyped]

from unbake.cli.common import suggest
from unbake.layout import planner, port, split, symbol_identity, symbol_proof
from unbake.layout.symbol_identity import similarity_distribution
from unbake.project import setup
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
        assertions=layout.get("symbol_assertions", []),
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
                    "compiler_reference": project.compiler_reference(f.name),
                },
            )
            records.append(record)
        layout["versions"][v]["functions"] = records
        del image
    layout["versions"] = {v: layout["versions"][v] for v in project.versions}
    layout["items"] = planner.symbol_items(layout["versions"])
    for item in layout["items"].values():
        for record in item["placements"].values():
            record["evidence"]["holding_versions"] = item["versions"]
            record["evidence"]["name_source"] = item["versions"][0]
    data = symbol_identity.data_identity(
        images,
        {v: [replace(f, name=names[v][f.start]) for f in rows] for v, rows in ff.items()},
        project_data_tables(project),
        lambda f: port.object_path(project, f),
        layout.get("data_assertions", []),
        header_data_types(project),
    )
    layout["data_symbols"] = data
    layout["inputs_sha256"]["symbol_replan"] = planner.digest([placements, layout["items"], data])
    report = {
        "schema": 1,
        "data_symbols": data,
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
        f"{len(report['placements'])} placement names change; "
        f"data objects {report['data_symbols']['unified']}, "
        f"data renames {report['data_symbols']['rename_count']}, "
        f"contradictions {report['data_symbols']['contradictions_by_reason']}"
    )
    if confirm is None:
        print(f"OK(setup): review build/setup/symbol-proposal.json; setup --replan-symbols --confirm {token}")
        suggest(f"unbake setup --replan-symbols --confirm {token}")
        return ["symbol proposal ready; executable boundaries retained"]
    if confirm != token:
        raise Held("setup", "setup.symbol_proposal_stale: symbol proposal or project inputs changed")
    if not replacements and not report["data_symbols"]["objects"]:
        return ["symbol names unchanged"]
    data_changes = report.get("data_symbols", {}).get("renames", {})
    changed_data = {old for local in data_changes.values() for old in local}
    for source in project.src.rglob("*.c"):
        if any(re.search(r"\b" + re.escape(name) + r"\b", source.read_text()) for name in changed_data):
            raise Held(
                "setup", f"data.authored_source: {source.relative_to(project.root)}: review changed data identities"
            )
        text = source.read_text()
        if rewrite(text, replacements) != text or source.stem in replacements:
            raise Held(
                "setup",
                f"setup.symbol_authored_source: {source.relative_to(project.root)}: "
                "changed identities require reviewed C before replanning",
            )
    return publish(project, policy, replacements, report, inputs)


def publish(
    project: Project, policy: SetupPolicy, replacements: dict[str, str], report: dict[str, Any], inputs: dict[str, str]
) -> list[str]:
    """Prove one simultaneous symbol transaction and publish its layout evidence."""
    directory = project.build / "setup"
    directory.mkdir(parents=True, exist_ok=True)
    data = toml.loads((project.root / "config.toml").read_text())
    groups: dict[str, set[str]] = defaultdict(set)
    for v in project.versions:
        for f in port.functions(project, v):
            groups[replacements.get(f.name, f.name)].add(project.compiler_reference(f.name))
    affected = set(replacements) | set(replacements.values())
    units = {name: ident for name, ident in data.get("units", {}).items() if name not in affected}
    for name in set(replacements.values()):
        # Joined placements that disagree build with the default first; try ranks the rest.
        if len(groups[name]) == 1 and (ident := next(iter(groups[name]))) != project.default_compiler:
            units[name] = ident
    data.pop("units", None)
    if units:
        data["units"] = dict(sorted(units.items()))
    # Data identities rebind addresses per version, which only the full proof covers.
    fast = symbol_proof.available(project, replacements) and not report.get("data_symbols", {}).get("renames")
    with tempfile.TemporaryDirectory(prefix="symbol-proof-", dir=directory) as temporary:
        tree = Path(temporary) / "tree"
        setup._copy_inputs(project, tree, inputs)
        for v in project.versions:
            target = tree / project.version(v).split.relative_to(project.root)
            target.write_text(rewrite_layout(target.read_text(), replacements))
            target = tree / project.version(v).symbols.relative_to(project.root)
            target.write_text(data_symbols_text(target.read_text(), v, replacements, report.get("data_symbols", {})))
        header_names = {**replacements, **shared_data_renames(report.get("data_symbols", {}))}
        for header in (tree / path.relative_to(project.root) for path in project.include):
            for path in header.rglob("*.h"):
                path.write_text(rewrite(path.read_text(), header_names))
        removed = []
        if not fast:
            for source in (tree / project.src.relative_to(project.root)).rglob("*.c"):
                source.write_text(rewrite(source.read_text(), header_names))
                if source.stem in replacements:
                    target = source.with_name(replacements[source.stem] + ".c")
                    if target.exists() and target.read_bytes() != source.read_bytes():
                        raise Held("split", f"split.join.authored_source: {source.name}: joined C sources differ")
                    removed.append(source.relative_to(tree).as_posix())
                    source.replace(target)
        for v, version in report["layout"]["versions"].items():
            if "split_yaml" in version["evidence"]:
                version["evidence"]["split_yaml"] = rewrite_layout(version["evidence"]["split_yaml"], replacements)
            if "symbols_text" in version["evidence"]:
                version["evidence"]["symbols_text"] = data_symbols_text(
                    version["evidence"]["symbols_text"], v, replacements, report.get("data_symbols", {})
                )
            for provider in version["providers"]:
                provider["name"] = path_name(provider["name"], replacements)
                provider["owners"] = [replacements.get(owner, owner) for owner in provider["owners"]]
        (tree / "config.toml").write_text(toml.dumps(data))
        (tree / "docs/setup/layout.json").write_text(
            json.dumps(report["layout"], separators=(",", ":"), sort_keys=True) + "\n"
        )
        with (tree / "docs/setup/symbol-correspondence.json").open("w") as stream:
            json.dump({k: v for k, v in report.items() if k != "layout"}, stream, indent=2, sort_keys=True)
            stream.write("\n")
        if fast:
            from unbake.project import config

            staged = config.load(tree)
            setup.run(staged, policy)
            # Header changes are identifier substitutions only. Authored C
            # references and edited compiled headers force the full proof in
            # available(); retained object bindings are checked and relinked
            # below, without reparsing an unchanged SDK type context.
            if replacements:
                receipts, assembly, generations = symbol_proof.prove(project, staged, policy, replacements)
            else:
                receipts = [
                    f"{v}: name-only proof; SHA1 OK; unchanged bindings and cached cartridge reused"
                    for v in project.versions
                ]
                assembly = []
                generations = setup._generations(project, project.versions)
            setup._publish(
                project,
                staged,
                inputs,
                fresh=True,
                generations=generations,
                assembly_changes=assembly,
                relocated_generations=True,
                reuse_generations=not replacements,
            )
            return [*receipts, "ready: name-only symbol transaction published"]
        del report, data, groups
        # Symbol updates rebuild every object. Prove versions in turn so
        # simultaneous large linkers do not multiply resident cartridge work.
        # The per-version build still uses the configured core budget.
        proof_policy = replace(policy, setup_version_jobs=1)
        return setup._prove_publish(
            project, tree, proof_policy, inputs, fresh=True, supply=None, removed_inputs=tuple(removed)
        )


def data_symbols_text(text: str, version: str, replacements: dict[str, str], data: dict[str, Any]) -> str:
    """Rename declarations locally and bind every proved object to its own address."""
    local = {**replacements, **data.get("renames", {}).get(version, {})}
    lines = []
    present = set()
    for line in text.splitlines(keepends=True):
        match = split.SYMBOL.match(line)
        if match:
            original = cast(str, match["name"])
            name = local.get(original, original)
            line = line[: match.start("name")] + name + line[match.end("name") :]
            present.add(name)
        lines.append(line)
    result = "".join(lines)
    for obj in data.get("objects", []):
        for placement in obj["placements"]:
            if placement["version"] == version and obj["name"] not in present:
                if result and not result.endswith("\n"):
                    result += "\n"
                result += f"{obj['name']} = 0x{placement['address']:08X};\n"
                present.add(obj["name"])
    return result


def shared_data_renames(data: dict[str, Any]) -> dict[str, str]:
    """A shared declaration has no version: only unambiguous spellings can move."""
    destinations: dict[str, set[str]] = defaultdict(set)
    for obj in data.get("objects", []):
        for placement in obj["placements"]:
            destinations[placement["symbol"]].add(obj["name"])
    return {old: next(iter(names)) for old, names in destinations.items() if len(names) == 1 and old not in names}


def header_data_types(project: Project) -> dict[str, str]:
    """Known shared declarations are kind evidence, including array/pointer shape."""
    result = {}
    pattern = re.compile(r"^\s*extern\s+(?P<type>[^;()]+?)\b(?P<name>[A-Za-z_]\w*)\s*(?P<suffix>\[[^\]]*\])?\s*;", re.M)
    for directory in project.include:
        for path in sorted(directory.rglob("*.h")):
            for match in pattern.finditer(path.read_text()):
                result[match["name"]] = " ".join((match["type"] + (match["suffix"] or "")).split())
    return result


def project_data_tables(project: Project) -> dict[str, dict[str, symbol_identity.DataSymbol]]:
    """Reserve generated labels too, including unshared and unreferenced objects.

    Disassembler extents are not object sizes. Only explicit metadata supplies
    size/kind evidence; generated tables supply names and proved addresses.
    """
    tables = {v: symbol_identity.data_table(split.read(project.version(v).symbols)) for v in project.versions}
    for version, table in tables.items():
        dump = project.build_link(version) / "splat_symbols.csv"
        if dump.is_file():
            with dump.open(newline="") as stream:
                for row in csv.DictReader(stream):
                    name = row["name"]
                    if split.NAME.fullmatch(name):
                        table.setdefault(name, symbol_identity.DataSymbol(int(row["vram_start"], 16)))
        addresses = project.build_link(version) / "symbol-addresses.txt"
        if addresses.is_file():
            # Extraction writes one "name address" pair per line.
            for line in addresses.read_text().splitlines():
                words = line.split()
                if len(words) == 2 and split.NAME.fullmatch(words[0]):
                    table.setdefault(words[0], symbol_identity.DataSymbol(int(words[1], 0)))
    return tables
