"""Submission intake and the single project landing drain."""
from __future__ import annotations

import json
import os
from collections import Counter
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import asdict
from hashlib import sha256
from pathlib import Path

from unbake import config as configuration
from unbake import effort, layout, policy, publish, store
from unbake.contracts import Config, Finding, Json, Proof, Receipt, Refusal, Submission, UnitSpec, digest

_ATTEMPTS = 3  # a commit that finds HEAD moved is measured again, up to this many times

def _read(path: Path) -> Submission:
    value = json.loads(path.read_text(encoding="utf-8"))
    configuration.validate("submission", value, str(path))
    value["proofs"] = tuple(Proof(**{**p, "missing": tuple(p["missing"])}) for p in value["proofs"])
    value["withheld"] = tuple(value["withheld"])
    return Submission(**value)

def _enqueue(config: Config, request: Json, member: str, operation: str, source: bytes,
             proofs: Sequence[Proof], origin: str, base: str, extras: Mapping[str, bytes],
             withheld: Sequence[str]) -> Submission:
    source_hash = sha256(source).hexdigest()
    identity = digest((member, source_hash, request["overrides"], operation,
                       *([sorted((p, sha256(b).hexdigest()) for p, b in extras.items())] if extras else []),
                       *([tuple(withheld)] if withheld else [])))
    inbox_path = config.project.root / ".unbake/inbox"
    for directory in (inbox_path, inbox_path / "done"):  # a refused entry is tried again: its refusal may be stale
        path = directory / f"{identity}.json"
        if path.is_file():
            return _read(path)
    source_path = f".unbake/inbox/{identity}.c"
    entry = Submission(identity, operation, member, request["function"], source_path, source_hash,
                       request["overrides"], base, tuple(proofs), origin, request["note"], effort.invocation(),
                       {path: f".unbake/inbox/{identity}.x{n}" for n, path in enumerate(sorted(extras))},
                       tuple(withheld))
    value = json.loads(json.dumps(asdict(entry)))
    configuration.validate("submission", value, f".unbake/inbox/{identity}.json")
    store.write(config.project.root / source_path, source)
    for path, copy in entry.extras.items():
        store.write(config.project.root / copy, extras[path])
    store.write(inbox_path / f"{identity}.json", json.dumps(value, sort_keys=True).encode())
    return entry

def submit(config: Config, request: Json, unit: UnitSpec, proofs: Sequence[Proof], source: bytes, origin: str,
           extras: Mapping[str, bytes] | None = None, withhold: bool = False) -> Submission | None:
    """One submission. `extras` makes it a change set: project-relative path -> new text, landed with the source as one
    plan and one commit, every touched unit proved exact or the whole set refused. Only exact sets are submitted.
    `withhold` lets a single file whose proofs are exact in only some versions land as exact there, withheld in the
    others; with no exact version it is refused."""
    with effort.stage("land.submit"):
        member = request["function"] or unit.members[0]
        snapshot = layout.capture(config)
        current = snapshot.layout.units.get(unit.path)
        kinds = configuration.load_resource("units.toml")["kind"]
        path = config.project.root / unit.path
        extras = extras or {}
        if (current and kinds[current.kind]["decompiled"] and path.is_file() and path.read_bytes() == source
                and not extras):
            return None
        exact = {p.version for p in proofs if all(q.exact for q in proofs if q.version == p.version)}
        withheld = sorted({p.version for p in proofs} - exact) if withhold and proofs and not extras else []
        if withhold and proofs and not exact:
            raise Refusal(Finding("land.no_exact_version", f"{member} is exact in no version, so none can land.",
                                  unit=member, versions=tuple(sorted({p.version for p in proofs})),
                                  action="make the source byte-exact in at least one version"))
        if proofs and (all(p.exact for p in proofs) or withheld):
            operation = "publish"
        else:
            if extras:
                return None
            scores = snapshot.layout.fuzzy[member]["scores"] if member in snapshot.layout.fuzzy else {}
            before = min(scores.values()) if scores else 0.0
            gain = configuration.load_resource("flow.toml")["fuzzy"]["min_gain"]
            if not proofs or any(any(key in p.symptoms for key in ("compile_failed", "unresolved", "no_measurement"))
                                 for p in proofs) or min(p.score for p in proofs) - before < gain:
                return None
            operation = "fuzzy"
        return _enqueue(config, request, member, operation, source, proofs, origin, snapshot.commit, extras, withheld)

def _move(config: Config, identity: str, destination: str) -> None:
    root = config.project.root / ".unbake/inbox"
    target = root / destination
    target.mkdir(parents=True, exist_ok=True)
    for path in root.glob(f"{identity}.*"):
        if path.suffix != ".lock":
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
        with store.exclusive(config, "land", wait=False) as free:
            busy = not free
        while not busy and (entries := inbox(config)):
            claimed = False
            for entry in entries:
                with store.exclusive(config, f"inbox/{entry.id}", wait=False) as mine:
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
