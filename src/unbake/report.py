"""Layout credit, cracking work lists, objdiff reports and README progress."""
from __future__ import annotations

import hashlib
import json
import math
import re
import struct
from collections import defaultdict

from unbake import config as configuration
from unbake import crack, draft, effort, land, layout, pool, store
from unbake.contracts import Config, Finding, Json, Refusal, Snapshot


def _pct(part: float, whole: float) -> float:
    return round(100 * part / whole, 2) if whole else 0.0

def _f32(value: float) -> float:
    single = struct.unpack(">f", struct.pack(">f", value))[0]
    for digits in range(1, 10):
        shortest = float(f"{single:.{digits}g}")
        if struct.unpack(">f", struct.pack(">f", shortest))[0] == single:
            return shortest
    return float(single)

def _subsystems() -> Json:
    return {r["id"]: r for r in configuration.load_resource("subsystems.toml")["subsystem"]}

def _inventory(snapshot: Snapshot):
    kinds = configuration.load_resource("units.toml")["kind"]
    for name, member in snapshot.layout.members.items():
        if (unit := layout.unit_of(snapshot, name)) is None and member.state == "bin":
            continue  # opaque ROM bytes are neither code nor data
        matched = unit is not None and kinds[unit.kind]["decompiled"] and not unit.withheld  # built in every version
        yield name, member, unit, matched, snapshot.layout.groups[member.group].subsystem if member.group else "unknown"

def _tally(b: Json) -> Json:
    return {**b, "fuzzy_bytes": (fuzzy := b["code_fuzzy"] + b["data_fuzzy"]),
            "code_percent": _pct(b["code_matched"], b["code_total"]),
            "data_percent": _pct(b["data_matched"], b["data_total"]),
            "fuzzy_percent": _pct(b["code_matched"] + b["data_matched"] + fuzzy, b["code_total"] + b["data_total"])}

def current(snapshot: Snapshot) -> Json:
    with effort.stage("report.current"):
        zero = dict.fromkeys(("code_total", "code_matched", "data_total", "data_matched",
                              "functions_total", "functions_matched", "code_fuzzy", "data_fuzzy"), 0)
        totals = {v: dict(zero) for v in snapshot.config.project.versions}
        subsystems, kinds = defaultdict(dict), defaultdict(dict)
        for name, member, unit, matched, subsystem in _inventory(snapshot):
            for v in member.holders():
                placements = [p for p in member.placements if p.version == v]
                code = sum(p.size for p in placements if p.section == ".text")
                data = sum(p.size for p in placements if p.section in (".data", ".rodata"))
                score = 0.0 if matched else (snapshot.layout.fuzzy.get(name) or {"scores": {v: 0.0}})["scores"][v]
                for bucket in (totals[v], subsystems[subsystem].setdefault(v, dict(zero)),
                               kinds[unit.kind if unit else member.state].setdefault(v, dict(zero))):
                    for prefix, size in (("code_", code), ("data_", data),
                                         ("functions_", int(member.kind == "function"))):
                        bucket[prefix + "total"] += size
                        bucket[prefix + "matched"] += size if matched else 0
                    bucket["code_fuzzy"] += round(score * code)
                    bucket["data_fuzzy"] += round(score * data)
        debt = {"stale": True, "action": "unbake check"}
        try:
            document = json.loads(snapshot.read(".unbake/check.json"))
            if document["commit"] == snapshot.commit:
                debt = {"commit": document["commit"], "counts": document["counts"]}
        except (FileNotFoundError, ValueError, KeyError, TypeError):
            pass
        counts = layout.boundary_plan(snapshot)[1]
        boundary = {key: counts[key] for key in ("prelude", "split", "merge")}
        return {"commit": snapshot.commit, "versions": {v: _tally(b) for v, b in totals.items()},
                "subsystems": {s: {v: _tally(b) for v, b in rows.items()} for s, rows in subsystems.items()},
                "kinds": {k: {v: _tally(b) for v, b in rows.items()} for k, rows in kinds.items()},
                "debt": debt, "boundary": boundary,
                "pending_boundary": sum(row["applied"] for row in boundary.values())}

def items(snapshot: Snapshot, params: Json) -> list[Json]:
    with effort.stage("report.items"):
        subsystems = _subsystems()
        subsystem, shard, count = (params.get(k) for k in ("subsystem", "shard", "count"))
        if subsystem is not None and (not isinstance(subsystem, str) or subsystem not in subsystems):
            raise Refusal(Finding("report.request", "subsystem must be a subsystems.toml id"))
        if count is not None and (type(count) is not int or count < 1):
            raise Refusal(Finding("report.request", "count must be an integer at least 1"))
        index, modulus = 0, 1
        if shard is not None and (not isinstance(shard, str) or not (m := re.fullmatch(r"([0-9]+)/([0-9]+)", shard))
                                  or not 0 <= (index := int(m[1])) < (modulus := int(m[2]))):
            raise Refusal(Finding("report.request", "shard must be I/K with 0 <= I < K"))
        rows = []
        for name, member, unit, matched, sid in _inventory(snapshot):
            sections = (".text",) if member.kind == "function" else (".data", ".rodata")
            if matched or not any(p.section in sections for p in member.placements):
                continue
            if member.kind == "function" and draft.split_slot(snapshot, name) is not None:
                continue
            if subsystem is not None and sid != subsystem:
                continue
            if int(hashlib.sha256(name.encode()).hexdigest()[:8], 16) % modulus != index:
                continue
            version = member.reference(snapshot.config.project.names_from)
            placement = next(p for p in member.placements if p.version == version)
            rows.append({"member": name, "kind": member.kind, "subsystem": sid, "rank": subsystems[sid]["rank"],
                         "state": crack.state(snapshot, name), "size": placement.size, "address": placement.vram,
                         "source": unit.path if unit is not None and unit.withheld else ""})
        rows.sort(key=lambda r: (r["size"] < 16, r["rank"], ("open", "tool", "fuzzy", "creative").index(r["state"]),
                                 -r["size"], r["address"], r["member"]))  # biggest first, fragments last
        seconds = configuration.load_resource("flow.toml")["work"]["permuter_seconds"]
        for row in rows[:count]:
            name = row["member"]
            row["best"] = (min(snapshot.layout.fuzzy[name]["scores"].values()) if row["state"] == "fuzzy"
                           else max((a.score for a in crack.history(snapshot.config, name)), default=0.0))
            source = row.pop("source")  # a withheld member has source: compare it, never crack it
            row["command"] = (f"unbake compare {source} --function {name}" if source
                              else f"unbake crack {name} --seconds {seconds}" if row["state"] in ("open", "tool")
                              else f"unbake compare .unbake/work/{store.stem(name)}.c --function {name}")
            if row["state"] == "creative":
                row["packet"] = f".unbake/packets/{store.stem(name)}.json"
        return rows[:count]

def _sum_measures(units: list[Json]) -> Json:
    counters = ("total_code", "matched_code", "complete_code", "total_data", "matched_data", "complete_data",
                "total_functions", "matched_functions", "total_units", "complete_units")
    result = {k: sum(int(u["measures"].get(k, 0)) for u in units) for k in counters}
    for field in ("matched_code", "complete_code", "matched_functions", "matched_data", "complete_data"):
        total = result["total_" + field.split("_", 1)[1]]
        result[field + "_percent"] = _f32(100 * result[field] / total) if total else 0.0
    weighted = sum(int(u["measures"].get("total_code", 0)) * u["measures"].get("fuzzy_match_percent", 0)
                   for u in units)
    result["fuzzy_match_percent"] = _f32(weighted / result["total_code"]) if result["total_code"] else 0.0
    for field in counters[:6]:
        result[field] = str(result[field])
    return result

def objdiff(snapshot: Snapshot, report: Json, version: str) -> Json:
    with effort.stage("report.objdiff"):
        units, subsystems = [], _subsystems()
        for name, member, unit, matched, sid in _inventory(snapshot):
            placements = [p for p in member.placements if p.version == version]
            if not placements:
                continue
            function = member.kind == "function"
            sections = (".text",) if function else (".data", ".rodata")
            size = sum(p.size for p in placements if p.section in sections)
            fuzzy = snapshot.layout.fuzzy.get(name) if not matched else None
            percent = 100.0 if matched else _f32(fuzzy["scores"][version] * 100) if fuzzy else 0.0
            counter = "total_code" if function else "total_data"
            prefix = counter.removeprefix("total_")
            measures = {counter: str(size), "total_units": 1}
            if function:
                measures.update(total_functions=1, fuzzy_match_percent=percent)
            if matched:
                for key in ("matched_", "complete_"):
                    measures[key + prefix] = str(size)
                    measures[key + prefix + "_percent"] = 100.0
                measures["complete_units"] = 1
                if function:
                    measures.update(matched_functions=1, matched_functions_percent=100.0)
            metadata = {"complete": matched, "progress_categories": [sid]}
            if matched or fuzzy:
                metadata["source_path"] = unit.path if matched else fuzzy["path"]
            address = next((p.vram for p in placements if p.section == ".text"), placements[0].vram)
            functions = [{"name": name, "size": str(size), "address": str(address), "metadata": {}}]
            if matched or fuzzy:
                functions[0]["fuzzy_match_percent"] = percent
            sections = ([{"name": ".text", "size": str(size), "fuzzy_match_percent": percent, "metadata": {}}]
                        if function else [{"name": p.section, "size": str(p.size), "fuzzy_match_percent": percent,
                                           "metadata": {}} for p in placements])
            units.append({"name": name, "measures": measures, "sections": sections,
                          "functions": functions if function else [], "metadata": metadata})
        addresses = {name: min(p.vram for p in m.placements if p.version == version)
                     for name, m in snapshot.layout.members.items() if version in m.holders()}
        units.sort(key=lambda u: (addresses[u["name"]], u["name"]))
        present = {u["metadata"]["progress_categories"][0] for u in units}
        categories = [{"id": sid, "name": subsystems[sid]["label"],
                       "measures": _sum_measures([u for u in units if sid in u["metadata"]["progress_categories"]])}
                      for sid in sorted(present, key=lambda s: (subsystems[s]["rank"], s))]
        result = {"measures": _sum_measures(units), "units": units, "categories": categories, "version": 2}
        configuration.validate("objdiff", result, f"versions/{version}/report.json")
        return result

def _line(label: str, bucket: Json, cells: int, kind: str = "bytes") -> str:
    parts = ("code", "data") if kind == "bytes" else (kind,)
    matched = sum(bucket[p + "_matched"] for p in parts)
    total = sum(bucket[p + "_total"] for p in parts)
    percent = _pct(matched, total)
    fuzzy = percent if kind == "functions" else _pct(matched + sum(bucket[p + "_fuzzy"] for p in parts), total)
    solid = int(percent // (100 / cells))
    partial = min(cells - solid, math.ceil(max(0, fuzzy - percent) / (100 / cells)))
    bar = "█" * solid + "▒" * partial + "░" * (cells - solid - partial)
    suffix = "" if kind == "functions" else f" (~{fuzzy:.2f}%)"
    return f"{label} [{bar}]  {percent:5.2f}%{suffix}  {matched:,} of {total:,}"

def readme(text: str, snapshot: Snapshot, report: Json) -> str:
    with effort.stage("report.readme"):
        settings = configuration.load_resource("repo.toml")["readme"]
        begin, end, cells = (settings[k] for k in ("begin", "end", "bar"))
        if text.count(begin) != 1 or text.count(end) != 1 or text.index(begin) >= text.index(end):
            raise Refusal(Finding("repo.readme_markers", "README.md needs one begin and one end progress marker",
                                  path="README.md"))
        project, versions = snapshot.config.project, report["versions"]
        total = {k: sum(versions[v][k] for v in project.versions) for k in
                 ("code_total", "code_matched", "data_total", "data_matched", "functions_total",
                  "functions_matched", "code_fuzzy", "data_fuzzy")}
        width = max(map(len, ("all", *project.versions))) + 1
        lines = [_line(label.ljust(width), bucket, cells) + " bytes"
                 for label, bucket in [("all", total), *((v, versions[v]) for v in project.versions)]]
        blocks = ["<pre><code>" + "</code><br><code>".join(lines) + "</code></pre>"]
        for v in project.versions:
            meta = project.version_files[v].meta
            details = ", ".join(meta[k] for k in ("cartridge_id", "region") if meta.get(k))
            header = f"| {v}" + (f" ({details})" if details else "") + "."
            header += (f" {meta['description']}" if meta.get("description") else "")
            header += f" SHA256 `{snapshot.versions[v].rom_sha256}` |"
            rows = [_line(k.ljust(9), versions[v], cells, k) for k in ("code", "data", "functions")]
            blocks.append(header + "\n|---|\n| <pre><code>" + "</code><br><code>".join(rows) + "</code></pre> |")
        return text[:text.index(begin) + len(begin)] + "\n" + "\n\n".join(blocks) + "\n" + text[text.index(end):]

def _objdiff_job(item) -> bytes:
    snapshot, report, version = item
    return json.dumps(objdiff(snapshot, report, version), sort_keys=True, separators=(",", ":")).encode() + b"\n"

def files(snapshot: Snapshot) -> dict[str, bytes]:
    with effort.stage("report.files"):
        report = current(snapshot)
        ids = snapshot.config.project.versions
        blobs = pool.map(snapshot.config, "report.objdiff", _objdiff_job, [(snapshot, report, v) for v in ids])
        result = {f"versions/{v}/report.json": blob for v, blob in zip(ids, blobs, strict=True)}
        result["README.md"] = readme(snapshot.read("README.md").decode(), snapshot, report).encode()
        return result

def run(config: Config, params: Json) -> Json:
    with effort.stage("report.run"):
        snapshot = layout.capture(config)
        if params["next"]:
            return {"items": items(snapshot, params)}
        return {**current(snapshot), "inbox": len(land.inbox(config)),
                "next": (items(snapshot, {**params, "count": 1}) or [{"command": ""}])[0]["command"]}
