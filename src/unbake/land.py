"""Submission intake and the single project landing drain."""
from __future__ import annotations

import json
import os
from collections import Counter
from collections.abc import Sequence
from dataclasses import asdict
from hashlib import sha256
from pathlib import Path

from unbake import build, compare, effort, infer, layout, native, policy, pool, publish, store, types
from unbake import config as configuration
from unbake.contracts import Config, Finding, Json, Proof, Receipt, Refusal, Snapshot, Submission, UnitSpec, digest

_ATTEMPTS = 3  # a commit that finds HEAD moved is measured again, up to this many times

def _read(path: Path) -> Submission:
    value = json.loads(path.read_text(encoding="utf-8"))
    configuration.validate("submission", value, str(path))
    value["proofs"] = tuple(Proof(**{**p, "missing": tuple(p["missing"])}) for p in value["proofs"])
    return Submission(**value)

def _enqueue(config: Config, request: Json, member: str, operation: str, source: bytes,
             proofs: Sequence[Proof], origin: str, base: str) -> Submission:
    source_hash = sha256(source).hexdigest()
    identity = digest((member, source_hash, request["overrides"], operation))
    inbox_path = config.project.root / ".unbake/inbox"
    for directory in (inbox_path, inbox_path / "done"):  # a refused entry is tried again: its refusal may be stale
        path = directory / f"{identity}.json"
        if path.is_file():
            return _read(path)
    source_path = f".unbake/inbox/{identity}.c" if operation != "withdraw" else ""
    entry = Submission(identity, operation, member, request["function"], source_path, source_hash,
                       request["overrides"], base, tuple(proofs), origin, request["note"], effort.invocation())
    value = json.loads(json.dumps(asdict(entry)))
    configuration.validate("submission", value, f".unbake/inbox/{identity}.json")
    if source_path:
        store.write(config.project.root / source_path, source)
    store.write(inbox_path / f"{identity}.json", json.dumps(value, sort_keys=True).encode())
    return entry

def submit(config: Config, request: Json, unit: UnitSpec, proofs: Sequence[Proof],
           source: bytes, origin: str) -> Submission | None:
    with effort.stage("land.submit"):
        member = request["function"] or unit.members[0]
        snapshot = layout.capture(config)
        current = snapshot.layout.units.get(unit.path)
        kinds = configuration.load_resource("units.toml")["kind"]
        path = config.project.root / unit.path
        if current and kinds[current.kind]["decompiled"] and path.is_file() and path.read_bytes() == source:
            return None
        if proofs and all(p.exact for p in proofs):
            operation = "publish"
        else:
            scores = snapshot.layout.fuzzy[member]["scores"] if member in snapshot.layout.fuzzy else {}
            before = min(scores.values()) if scores else 0.0
            gain = configuration.load_resource("flow.toml")["fuzzy"]["min_gain"]
            if not proofs or any(any(key in p.symptoms for key in ("compile_failed", "unresolved", "no_measurement"))
                                 for p in proofs) or min(p.score for p in proofs) - before < gain:
                return None
            operation = "fuzzy"
        return _enqueue(config, request, member, operation, source, proofs, origin, snapshot.commit)

def _move(config: Config, identity: str, destination: str) -> None:
    root = config.project.root / ".unbake/inbox"
    target = root / destination
    target.mkdir(parents=True, exist_ok=True)
    for suffix in (".c", ".json"):
        path = root / (identity + suffix)
        if path.exists():
            os.replace(path, target / path.name)

def inbox(config: Config) -> list[Submission]:
    with effort.stage("land.inbox"):
        entries = []
        paths = sorted((config.project.root / ".unbake/inbox").glob("*.json"),
                       key=lambda path: (path.stat().st_mtime_ns, path.name))
        for path in paths:
            try:
                entries.append(_read(path))
            except (Refusal, ValueError, TypeError, KeyError, OSError) as error:
                finding = Finding("inbox.corrupt", str(error), path=str(path.relative_to(config.project.root)))
                _move(config, path.stem, "refused")
                store.log(config, "refusal", {"id": path.stem, "findings": [asdict(finding)]})
        return entries

def _land(config: Config, entry: Submission) -> Receipt:
    """Measuring takes no lock, only the commit does (journal.apply): a HEAD that moved under it means measure again."""
    for attempt in range(_ATTEMPTS):
        try:
            return publish.land(config, entry)
        except Refusal as error:
            if attempt == _ATTEMPTS - 1 or not any(f.key == "journal.changed" for f in error.findings):
                raise

def drain(config: Config) -> Json:
    """Lands the inbox: one lock per entry so drains run side by side, the project lock only probed."""
    with effort.stage("land.drain"):
        landed, refused, busy = [], [], False
        with store.exclusive(config, "land") as free:
            busy = not free
        while not busy and (entries := inbox(config)):
            claimed = False
            for entry in entries:
                with store.exclusive(config, f"inbox/{entry.id}") as mine:
                    if not mine or not (config.project.root / f".unbake/inbox/{entry.id}.json").exists():
                        continue
                    claimed = True
                    with effort.stage("land.entry"):
                        body = {"id": entry.id, "member": entry.member}
                        try:
                            receipt = _land(config, entry)
                        except Refusal as error:
                            body["findings"] = [asdict(f) for f in error.findings]
                            refused.append(body)
                            store.log(config, "refusal", body)
                            _move(config, entry.id, "refused")
                        else:
                            landed.append({**body, "operation": receipt.operation, "commit": receipt.commit})
                            store.log(config, "withdrawal" if receipt.operation == "withdraw" else "receipt",
                                      {**body, **asdict(receipt)})
                            _move(config, entry.id, "done")
                (config.project.root / f".unbake/inbox/{entry.id}.lock").unlink(missing_ok=True)
            busy = not claimed  # what is left belongs to other drains
        return {"running": busy, "landed": landed, "refused": refused}

def submit_command(config: Config, params: Json) -> Json:
    with effort.stage("land.submit_command"):
        files = params["files"]
        if not files or (params["function"] and len(files) != 1):
            raise Refusal(Finding("land.request", "Supply files and use --function with exactly one file."))
        submissions = []
        for file in files:
            snapshot = layout.capture(config)
            request = {"file": str(file), "function": params["function"], "overrides": {"add": [], "omit": []},
                       "note": params["note"] or ""}
            unit, bound = compare.bind(snapshot, Path(file), params["function"])
            proofs = compare.measure(bound, unit, request["overrides"], None)
            request["function"] = request["function"] or unit.members[0]
            entry = submit(config, request, unit, proofs, Path(file).read_bytes(), "submit")
            submissions.append({"file": str(file), "member": request["function"], "id": entry.id if entry else None,
                                "operation": entry.operation if entry else "none",
                                "exact": all(p.exact for p in proofs), "score": min(p.score for p in proofs),
                                "findings": [asdict(f) for f in compare.gaps(bound, unit, proofs)]})
        return {"submissions": submissions, "drain": drain(config)}

def land_command(config: Config, params: Json) -> Json:
    with effort.stage("land.land_command"):
        return drain(config)

def _prove_job(job: tuple[Snapshot, UnitSpec, str]) -> tuple[Proof, ...] | Refusal:
    with effort.stage("land.reprove"):
        try:
            return native.prove_job(job)
        except Refusal as error:
            return error

def _samples(snapshot: Snapshot, path: Path) -> Json:
    """Known cross-version pairs and known types: how many the tool resolves, and the ones it misses."""
    if not path.is_file():
        raise Refusal(Finding("config.missing", f"the samples file {path} does not exist", path=str(path)))
    wanted = json.loads(path.read_text())
    found = infer.pairs(snapshot, sorted({(r["from"], r["to"]) for r in wanted["pairs"]}))
    rows = [{**r, "found": found[r["from"], r["to"]].get(r["from_member"])} for r in wanted["pairs"]]
    known = types.load(snapshot)
    names = [n for n in wanted["types"] if not any(n in known[kind] for kind in ("function", "global", "struct"))]
    return {"pairs": {"hits": sum(r["found"] == r["to_member"] for r in rows), "total": len(rows),
                      "misses": [r for r in rows if r["found"] != r["to_member"]]},
            "types": {"hits": len(wanted["types"]) - len(names), "total": len(wanted["types"]), "misses": names}}

def check_command(config: Config, params: Json) -> Json:
    with effort.stage("land.check_command"):
        snapshot = layout.capture(config)
        findings = policy.census(snapshot)
        counts = dict(sorted(Counter(f.key for f in findings).items()))
        store.write(config.project.root / ".unbake/check.json", json.dumps(
            {"commit": snapshot.commit, "counts": counts, "findings": [asdict(f) for f in findings]},
            sort_keys=True).encode())
        reprove, repair, drained = {"exact": [], "withheld": [], "debt": []}, {"submitted": 0, "withdraw": 0}, None
        if params["both"] and not params["reprove"]:
            raise Refusal(Finding("check.both", "--both compares the Makefile build with the reprove: pass --reprove"))
        jobs = outcomes = []
        if params["reprove"]:
            kinds = configuration.load_resource("units.toml")["kind"]
            jobs = [(snapshot, unit, version) for _, unit in sorted(snapshot.layout.units.items())
                    if kinds[unit.kind]["decompiled"] for version in compare.holders(snapshot, unit)]
            if params["unit"]:
                wanted = sorted(set(params["unit"]))
                units = snapshot.layout.units
                bad = [Finding("check.unit", f"--unit {u} is not a decompiled unit of this project", path=u)
                       for u in wanted if u not in units or not kinds[units[u].kind]["decompiled"]]
                if bad:
                    raise Refusal(*bad)
                jobs = [job for job in jobs if job[1].path in wanted]
            identities = {}
            outcomes = pool.map(config, "land.reprove", _prove_job, jobs, lambda j: native.stamp(j[0], j[1], j[2]))
            for (_, unit, version), outcome in zip(jobs, outcomes, strict=True):
                proofs = {} if isinstance(outcome, Refusal) else {p.member: p for p in outcome}
                for member in unit.members:
                    if version not in snapshot.layout.members[member].holders():
                        continue
                    proof = proofs.get(member)
                    identity = {"member": member, "version": version}
                    missing = list(proof.missing) if proof else ([f"version {version}: {f.key}: {f.reason}"
                        for f in outcome.findings] if isinstance(outcome, Refusal) else [])
                    exact = bool(proof and proof.exact)
                    identities[member, version] = (exact, identity if exact else {
                        **identity, "missing": missing or [f"version {version}: no measurement"]})
            for (member, version), (exact, identity) in identities.items():
                unit = next(u for _, u, v in jobs if v == version and member in u.members)
                reprove["exact" if exact else "withheld" if version in unit.withheld else "debt"].append(identity)
        proved = None
        if params["both"]:
            proved = build.both_paths(snapshot, [(u, v) for _, u, v in jobs if v not in u.withheld], {
                (p.member, v): p for (_, _, v), o in zip(jobs, outcomes, strict=True)
                if not isinstance(o, Refusal) for p in o})
        if params["repair"]:
            for _, finding in sorted({(f.path, f.key): f for f in findings if not f.blocking}.items()):
                unit = snapshot.layout.units.get(finding.path)
                if unit is None:
                    continue
                writes = policy.repair(snapshot, finding)
                source = writes.get(unit.path) if writes is not None else None
                operation = "repair" if source is not None else "withdraw"
                request = {"function": None, "overrides": {"add": [], "omit": []}, "note": finding.reason}
                _enqueue(config, request, unit.members[0], operation, source if source is not None else b"",
                         (), "check", snapshot.commit)
                repair["submitted" if operation == "repair" else "withdraw"] += 1
            drained = drain(config)
        samples = _samples(snapshot, params["samples"]) if params["samples"] else None
        if params["strict"] and findings:
            raise Refusal(Finding("check.debt", f"{len(findings)} findings", missing=tuple(sorted(counts)),
                                  action="unbake check --repair"))
        return {"commit": snapshot.commit, "counts": counts, "reprove": reprove, "repair": repair, "drain": drained,
                "samples": samples, "both": proved}
