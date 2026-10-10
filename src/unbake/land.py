"""Submission intake and the single project landing drain."""
from __future__ import annotations

import json
import os
from collections import Counter
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import asdict
from hashlib import sha256
from pathlib import Path

from unbake import compare, effort, layout, policy, publish, store
from unbake import config as configuration
from unbake.contracts import Config, Finding, Json, Proof, Receipt, Refusal, Submission, UnitSpec, digest

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
    source_path = f".unbake/inbox/{identity}.c"
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
        entries, stamped = [], []
        for path in (config.project.root / ".unbake/inbox").glob("*.json"):
            with suppress(FileNotFoundError):  # another drain landed it between the listing and now
                stamped.append((path.stat().st_mtime_ns, path.name, path))
        for _, _, path in sorted(stamped):
            try:
                entries.append(_read(path))
            except FileNotFoundError:
                continue
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
                        except Exception as error:  # a crash must not leave the entry to fail again unseen
                            findings = error.findings if isinstance(error, Refusal) else (
                                Finding("internal.error", f"{type(error).__name__}: {error}", unit=entry.member),)
                            body["findings"] = [asdict(f) for f in findings]
                            refused.append(body)
                            store.log(config, "refusal", body)
                            _move(config, entry.id, "refused")
                        else:
                            landed.append({**body, "operation": receipt.operation, "commit": receipt.commit})
                            store.log(config, "receipt", {**body, **asdict(receipt)})
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

def check_command(config: Config, params: Json) -> Json:
    with effort.stage("land.check_command"):
        snapshot = layout.capture(config)
        findings = policy.census(snapshot)
        counts = dict(sorted(Counter(f.key for f in findings).items()))
        store.write(config.project.root / ".unbake/check.json", json.dumps(
            {"commit": snapshot.commit, "counts": counts, "findings": [asdict(f) for f in findings]},
            sort_keys=True).encode())
        if params["strict"] and findings:
            raise Refusal(Finding("check.debt", f"{len(findings)} findings", missing=tuple(sorted(counts)),
                                  action="fix each finding in the source"))
        return {"commit": snapshot.commit, "counts": counts}
