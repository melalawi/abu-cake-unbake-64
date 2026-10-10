"""Measured cracking attempts and packets for the creative ladder."""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from unbake import adapters, compare, draft, effort, land, layout, process, recipes, store, symptoms, types, versions
from unbake import config as configuration
from unbake.contracts import Attempt, Config, Finding, Json, Proof, Recipe, Refusal, Snapshot, UnitSpec


def _subsystem(snapshot: Snapshot, member: str) -> Json:
    group = snapshot.layout.members[member].group
    name = snapshot.layout.groups[group].subsystem if group else "unknown"
    return next(r for r in configuration.load_resource("subsystems.toml")["subsystem"] if r["id"] == name)
def _next(matched: list[Json], prior: list[Attempt], row: Json) -> str:
    tried = {hint for attempt in prior for hint in attempt.hints}
    for hint in matched:
        if hint["id"] not in tried:
            return hint["technique"]
    if row["idioms"]:
        return row["idioms"][0]
    return next(r["help"] for r in configuration.load_resource("flow.toml")["ladder"] if r["id"] == "creative")
def _attempts(member: str) -> str:
    return f"attempts/{store.stem(member)}"

def state(snapshot: Snapshot, member: str) -> str:
    if member in snapshot.layout.fuzzy:
        return "fuzzy"
    stem, config = store.stem(member), snapshot.config
    return "creative" if f"{stem}.json" in store.listing(config, "packets") else (
        "tool" if f"{stem}.jsonl" in store.listing(config, "attempts") else "open")

def history(config: Config, member: str) -> list[Attempt]:
    with effort.stage("crack.history"):
        return [Attempt(**{**row, "hints": tuple(row["hints"])}) for row in store.rows(config, _attempts(member))]

def feedback(snapshot: Snapshot, member: str, step: str, proofs: Sequence[Proof], note: str) -> tuple[Attempt, Json]:
    with effort.stage("crack.feedback"):
        mine = [p for p in proofs if p.member == member]
        score = min((p.score for p in mine), default=0.0)
        prior = history(snapshot.config, member)
        best_before = max((a.score for a in prior), default=0.0)
        failed = any(p.symptoms.get(k) for p in mine for k in ("compile_failed", "no_measurement", "unresolved"))
        outcome = "exact" if mine and all(p.exact for p in mine) else (
            "failed" if score == 0 and failed else "better" if score > best_before else (
                "same" if score == best_before else "worse"))
        facts = dict(min(mine, key=lambda p: p.score).symptoms) if mine else {}
        plateau = int(step == "options" and outcome in ("same", "worse"))
        for attempt in reversed(prior):
            if attempt.step != "options" or attempt.outcome not in ("same", "worse"):
                break
            plateau += 1
        if plateau:
            facts["plateau_probes"] = plateau
        row = _subsystem(snapshot, member)
        matched, _ = draft.hints(snapshot, row["id"], facts)
        attempt = Attempt(member, step, mine[0].source_sha256 if mine else "", mine[0].recipe if mine else "",
                          score, best_before, outcome, facts, tuple(h["id"] for h in matched), row["id"],
                          note, effort.invocation(), datetime.now(UTC).isoformat())
        store.append(snapshot.config, _attempts(member), json.loads(json.dumps(asdict(attempt), sort_keys=True)))
        return attempt, {"member": member, "subsystem": row["id"], "step": step, "score_before": best_before,
                         "score_after": score, "outcome": outcome, "hints": matched,
                         "next": _next(matched, prior, row), "label": "CRACKED" if outcome == "exact" else ""}
def _missing() -> bytes:
    raise Refusal(Finding("store.corrupt", "measured candidate bytes are missing from the cache"))

def packet(snapshot: Snapshot, member: str) -> Path:
    with effort.stage("crack.packet"):
        config, root = snapshot.config, snapshot.config.project.root
        source = f".unbake/work/{store.stem(member)}.c"
        path = root / source
        if not path.exists():
            raise Refusal(Finding("land.request", f"{member} has no candidate",
                                  action=f"unbake crack {member} --seconds N"))
        unit, bound = compare.bind(snapshot, path, member)
        proofs = [p for p in compare.measure(bound, unit, {"add": [], "omit": []}, None) if p.member == member]
        item = snapshot.layout.members[member]
        row = _subsystem(snapshot, member)
        facts = symptoms.merge([p.symptoms for p in proofs])
        matched, hints = draft.hints(snapshot, row["id"], facts)
        prior = history(config, member)
        recipe = recipes.resolve(config, unit, {})
        target = {}
        for version in item.holders():
            place = next(p for p in item.placements if p.version == version)
            target[version] = {"asm": versions.asm_path(config, version, member).relative_to(root).as_posix(),
                               "size": place.size, "vram": place.vram}
        diff = ""
        measured = [p for p in proofs if p.built_sha256]  # a candidate that did not link has no built bytes
        if item.kind == "function" and measured:
            proof = min(measured, key=lambda p: p.score)
            place = next(p for p in item.placements if p.version == proof.version and p.section == ".text")
            built = store.cached(config, "bytes", proof.built_sha256, _missing)
            diff = symptoms.diff(built[:place.size], versions.rom_bytes(snapshot.versions[proof.version],
                                 place.rom_start, place.rom_end), place.vram)
        names = []
        if item.kind == "function":
            asm = versions.asm_path(config, item.reference(config.project.names_from), member).read_text()
            names = list(dict.fromkeys(a or b for a, b in re.findall(
                r"\bjal\s+([\w.$]+)|%(?:hi|lo)\(\s*([\w.$]+)\s*\)", asm)))
        kinds = configuration.load_resource("units.toml")["kind"]
        address = target.get(config.project.names_from, next(iter(target.values())))["vram"]
        siblings = []
        for sibling_unit in snapshot.layout.units.values():
            if not kinds[sibling_unit.kind]["decompiled"]:
                continue
            for name in sibling_unit.members:
                other = snapshot.layout.members[name]
                if name == member or _subsystem(snapshot, name)["id"] != row["id"]:
                    continue
                places = [p for p in other.placements if p.version == config.project.names_from]
                distance = abs((places or list(other.placements))[0].vram - address)
                siblings.append((other.group != item.group, distance, name, sibling_unit.path))
        body = {"schema": 1, "member": member, "kind": item.kind, "group": item.group,
                "subsystem": {k: row[k] for k in
                              ("id", "label", "idioms", "structs", "headers", "compiler", "sources")},
                "versions": list(item.holders()), "toolchain": recipe.toolchain,
                "recipe": {"cflags": list(recipe.cflags), "cppflags": list(recipe.cppflags)}, "target": target,
                "best": {"source": source, "score": {p.version: p.score for p in proofs}, "symptoms": facts},
                "diff": diff, "matched_hints": matched, "subsystem_hints": hints,
                "siblings": [{"member": n, "path": p} for _, _, n, p in sorted(siblings)
                             [:configuration.load_resource("flow.toml")["packet"]["siblings"]]],
                "types": {"signature": types.signature(snapshot, [member]).get(member, ""),
                          "declarations": list(types.declarations(snapshot, names).values())},
                "attempts": {"count": len(prior), "best": max((a.score for a in prior), default=0.0),
                             "steps": list(dict.fromkeys(a.step for a in prior)),
                             "plateau_probes": prior[-1].symptoms.get("plateau_probes", 0) if prior else 0},
                "next": _next(matched, prior, row),
                "commands": {verb: f"unbake {verb} {source} --function {member}" for verb in ("compare", "submit")}}
        path = root / f".unbake/packets/{store.stem(member)}.json"
        configuration.validate("packet", body, str(path))
        store.write(path, json.dumps(body, sort_keys=True).encode())
        return path
def _delta(base: Recipe, other: Recipe) -> Json:
    have = [*base.cppflags, *base.cflags, *base.asflags]
    want = [*other.cppflags, *other.cflags, *other.asflags]
    result = {"add": [t for t in want if t not in have], "omit": [t for t in have if t not in want]}
    if other.toolchain != base.toolchain:
        result["toolchain"] = other.toolchain
    return result
def _permute(config: Config, snap: Snapshot, unit: UnitSpec, member: str,
             recipe: Recipe, seconds: float) -> bytes | None:
    version = snap.layout.members[member].reference(config.project.names_from)
    with store.work(config) as work:
        (work / "base.c").write_bytes(snap.read(unit.path))
        include = [config.project.root / "asm" / version / "include", config.project.root / "include"]
        adapters.assembler(config, "gnu").assemble(
            versions.asm_path(config, version, member), recipe, include, work / "target.o")
        script = work / "compile.sh"
        script.write_text(adapters.script(config, recipe, version))
        script.chmod(0o755)
        process.run("permuter", [str(process.tool(config, "permuter", kind=unit.kind)), str(work), "--stop-on-zero",
                                 "-j", str(config.host.workers)],
                    cwd=work, timeout=seconds, tmp=process.scratch(config.project.root))
        outputs = [(int(match[1]), entry.name, entry / "source.c") for entry in work.iterdir()
                   if (match := re.fullmatch(r"output-(\d+)-\d+", entry.name)) and (entry / "source.c").is_file()]
        return min(outputs)[2].read_bytes() if outputs else None

def run(config: Config, params: Json) -> Json:
    with effort.stage("crack.run"):
        member, seconds = params["item"], params["seconds"]
        if seconds < 1:
            raise Refusal(Finding("land.request", "cracking requires at least one second"))
        snapshot = layout.capture(config)
        if member not in snapshot.layout.members:
            raise Refusal(Finding("land.request", f"{member} is unknown"))
        draft.refuse_fragment(snapshot, member)
        kinds = configuration.load_resource("units.toml")["kind"]
        existing = layout.unit_of(snapshot, member)
        if existing and kinds[existing.kind]["decompiled"]:
            missing = f"; its source is withheld in {', '.join(existing.withheld)}" if existing.withheld else ""
            raise Refusal(Finding("land.request", f"{member} already has source in {existing.path}{missing}",
                                  versions=tuple(existing.withheld), path=existing.path,
                                  action=f"unbake compare {existing.path} --function {member}"))
        cand = config.project.root / f".unbake/work/{store.stem(member)}.c"
        steps = []
        best_overrides = {"add": [], "omit": []}
        with effort.stage("crack.draft"):
            if not cand.exists():
                draft.create(snapshot, member, cand)
            unit, bound = compare.bind(snapshot, cand, member)
            proofs = compare.measure(bound, unit, best_overrides, None)
            attempt, _ = feedback(bound, member, "draft", proofs, "")
            steps.append({"step": attempt.step, "score": attempt.score, "outcome": attempt.outcome})
            best, exact = attempt.score, attempt.outcome == "exact"
        if not exact:
            with effort.stage("crack.options"):
                base = recipes.resolve(config, unit, {})
                deltas = [_delta(base, proposal) for proposal in recipes.proposals(config, base)]
                for overrides, measured in zip(deltas, compare.measure_many(bound, unit, deltas), strict=True):
                    attempt, _ = feedback(bound, member, "options", measured, "")
                    steps.append({"step": attempt.step, "score": attempt.score, "outcome": attempt.outcome})
                    if attempt.score > best or attempt.outcome == "exact":
                        best, best_overrides, proofs = attempt.score, overrides, measured
                    if exact := attempt.outcome == "exact":
                        break
        if not exact:
            with effort.stage("crack.permuter"):
                output = _permute(config, bound, unit, member,
                                  recipes.resolve(config, unit, best_overrides), float(seconds))
                if output is not None:
                    proposed = layout.overlay(bound, {unit.path: output})
                    measured = compare.measure(proposed, unit, best_overrides, None)
                    attempt, _ = feedback(proposed, member, "permuter", measured, "")
                    steps.append({"step": attempt.step, "score": attempt.score, "outcome": attempt.outcome})
                    exact = attempt.outcome == "exact"
                    if attempt.score > best or exact:
                        best, proofs, bound = attempt.score, measured, proposed
                        store.write(cand, output)
        packet_path = None if exact else str(packet(snapshot, member))
        submission = drain = None
        flow = configuration.load_resource("flow.toml")
        fuzzy = snapshot.layout.fuzzy.get(member)
        baseline = min(fuzzy["scores"].values()) if fuzzy else 0.0
        if not params.get("no_submit") and (exact or best - baseline >= flow["fuzzy"]["min_gain"]):
            request = {"file": str(cand), "function": member, "overrides": best_overrides, "note": ""}
            submission = land.submit(config, request, unit, proofs, cand.read_bytes(), "crack")
            if exact or submission is not None:
                drain = land.drain(config)
        uncompiled = next((m for p in proofs for m in p.missing if "compile.error" in m), "")
        return {"member": member, "steps": steps, "best": best, "state": "exact" if exact else "creative",
                "candidate": str(cand), "packet": packet_path, "compile_error": uncompiled,
                "submitted": submission.id if submission else None,
                "drain": drain, "label": "CRACKED" if exact else "NEEDS CREATIVE"}
