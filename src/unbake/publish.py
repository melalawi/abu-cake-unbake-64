"""Admission, submission landing and withdrawal."""
import json
from collections.abc import Sequence
from dataclasses import replace
from hashlib import sha256
from pathlib import Path

import yaml

from unbake import (
    build,
    compare,
    crack,
    effort,
    headers,
    journal,
    layout,
    native,
    ownership,
    policy,
    pool,
    recipes,
    repo,
    store,
    symptoms,
    types,
    versions,
    view,
)
from unbake import config as configuration
from unbake.contracts import (
    Config,
    Finding,
    Json,
    Plan,
    Proof,
    Receipt,
    Refusal,
    Snapshot,
    Submission,
    UnitSpec,
    digest,
)


def _objects(item):
    return native.objects(*item)[0]
def _measure(item):
    return native.measure(*item)
def _blocking(findings):
    findings = tuple(f for f in findings if f.blocking)
    if findings:
        raise Refusal(*findings)
def _new_findings(snapshot, path, findings, sdk):
    """Findings the landed text of path did not already carry: a landed unit's old debt does not block its next text."""
    if not any(f.blocking for f in findings):
        return
    old = snapshot.peek(path)
    before = policy.evaluate(snapshot, path, old.decode(), None, sdk) if old is not None else ()
    _blocking(policy.scope(before, findings, {path})[0])
def _plan(operation, snapshot, writes, affected, debt, message):
    body = (operation, snapshot.digest, writes, affected, (), debt, message)
    return Plan(*body, digest(body))
def _proofs(snapshot: Snapshot, affected: Sequence[str]) -> tuple[tuple[Proof, ...], tuple[Finding, ...]]:
    with store.work(snapshot.config) as work:
        jobs, units = [], []
        for path in affected:
            own, unit = ownership.derive(snapshot, snapshot.layout.units[path])  # its data is proved with its code
            units.append((own, unit))
            recipe = recipes.resolve(snapshot.config, unit, unit.options)
            for version in compare.holders(own, unit):
                jobs.append((own, unit, version, recipe, work / f"{len(jobs)}"))
        batches = pool.map(snapshot.config, "publish.prove", native.prove_args, jobs)
        proofs = tuple(p for batch in batches for p in batch)
        gaps = tuple(f for own, unit in units for f in compare.gaps(own, unit, proofs))
        return proofs, gaps
def _regressions(snapshot: Snapshot, gaps: tuple[Finding, ...], own: Sequence[str]) -> tuple[Finding, ...]:
    """Another member's gap blocks only in a version where HEAD proves it exact."""
    heads = {g.unit: layout.unit_of(snapshot, g.unit) for g in gaps if g.unit not in own}
    paths = sorted({u.path for u in heads.values() if u is not None})
    exact = {(p.member, p.version) for p in _proofs(snapshot, paths)[0] if p.exact} if paths else set()
    return tuple(g for g in gaps if g.unit in own or heads[g.unit] is None
                 or any((g.unit, v) in exact for v in g.versions))
def _consumers(snapshot, header, group=None):
    include = f'#include "{header.removeprefix("include/")}"'
    return tuple(path for path, unit in snapshot.layout.units.items()
                 if (group is None or unit.group == group)
                 and include in snapshot.read(path).decode())
def admit(snapshot: Snapshot, request: Json) -> tuple[UnitSpec, Snapshot, tuple[Proof, ...]]:
    with effort.stage("publish.admit"):
        with effort.stage("publish.admit.1"):
            file = Path(request["file"])
            file = file if file.is_absolute() else snapshot.config.project.root / file
            unit, snap = compare.bind(snapshot, file, request["function"])
            for member in unit.members:
                owner = layout.unit_of(snapshot, member)
                if (owner is not None and owner.path != unit.path
                        and snapshot.layout.members[member].state == unit.kind):
                    raise Refusal(Finding("land.request", f"Already published in {owner.path}.", unit=member))
            text = snap.read(unit.path).decode()
            group = snap.layout.groups[unit.group]
        with effort.stage("publish.admit.3"):
            _new_findings(snapshot, unit.path, policy.evaluate(snap, unit.path, text, None, group.sdk), group.sdk)
        with effort.stage("publish.admit.4"):
            recipe = recipes.resolve(snap.config, unit, request["overrides"])
            unit = replace(unit, toolchain=recipe.toolchain,
                           options={k: v for k, v in request["overrides"].items() if k != "toolchain"})
            snap, unit = ownership.derive(snap, unit)  # the data its source emits is proved with its code
            holders = compare.holders(snap, unit)
            views = view.all_versions(snap, unit, recipe, holders)
        with effort.stage("publish.admit.5"):
            _new_findings(snapshot, unit.path, policy.evaluate(snap, unit.path, text, views[holders[0]], group.sdk),
                          group.sdk)
        with store.work(snap.config) as work:
            with effort.stage("publish.admit.6"):
                jobs = [(snap, unit, v, recipe, work / f"{i}") for i, v in enumerate(holders)]
                objects = pool.map(snap.config, "publish.objects", _objects, jobs)
            with effort.stage("publish.admit.7"):
                unresolved = tuple(f for v, obj in zip(holders, objects, strict=True)
                                   for f in versions.resolve(snap.versions[v], versions.undefined(obj), unit.path))
                if unresolved:
                    raise Refusal(*unresolved)
            with effort.stage("publish.admit.8"):
                jobs = [(snap, unit, v, recipe, obj, work / f"{i}")
                        for i, (v, obj) in enumerate(zip(holders, objects, strict=True))]
                proofs = tuple(p for batch in pool.map(snap.config, "publish.measure", _measure, jobs) for p in batch)
                gaps = compare.gaps(snap, unit, proofs)
                if gaps:
                    raise Refusal(*gaps)
        return unit, snap, proofs
def _append(old, folded):
    existing, incoming = old.decode().splitlines(), folded.decode().splitlines()
    includes = [line for line in incoming if line.lstrip().startswith("#include") and line not in existing]
    position = max((i + 1 for i, line in enumerate(existing) if line.lstrip().startswith("#include")), default=0)
    existing[position:position] = list(dict.fromkeys(includes))
    remaining = [line for line in incoming if not line.lstrip().startswith("#include")]
    return ("\n".join(existing).rstrip() + "\n\n" + "\n".join(remaining).strip() + "\n").encode()
def _generated(snapshot, writes):
    overlay = layout.overlay(snapshot, writes)
    for path, data in repo.files(overlay).items():
        if data != snapshot.peek(path):
            writes[path] = data
def plans(snapshot: Snapshot, unit: UnitSpec, proposed: Snapshot) -> list[Plan]:
    with effort.stage("publish.plans"):
        recipe = recipes.resolve(proposed.config, unit, unit.options)
        first = compare.holders(proposed, unit)[0]
        owner = layout.unit_of(snapshot, unit.members[0])
        state = snapshot.layout.members[unit.members[0]].state
        again = owner is not None and owner.path == unit.path and (state == unit.kind or state.startswith("."))
        if again:  # its declarations were folded when it first landed: a new text keeps them where they are
            folded, conflicts = {unit.path: proposed.read(unit.path)}, ()
        else:
            folded, conflicts = headers.fold(proposed, unit, view.get(proposed, unit, first, recipe))
        if conflicts:
            raise Refusal(*conflicts)
        result, last = [], ()
        options = [(owner, {})] if again else layout.unit_options(snapshot, unit.members[0], folded[unit.path])
        for option, writes in options:
            try:
                writes = dict(writes)
                writes[option.path] = (_append(snapshot.read(option.path), folded[unit.path])
                                      if option.path in snapshot.layout.units and not again else folded[unit.path])
                writes.update((p, b) for p, b in folded.items() if p != unit.path)
                overlay = layout.overlay(snapshot, writes)
                configured = replace(option, toolchain=unit.toolchain, options=unit.options)
                gone = {k for k in snapshot.layout.units.keys() - proposed.layout.units.keys()  # data it now owns
                        if all(map(versions.unowned, snapshot.layout.units[k].members))}
                units = {k: u for k, u in overlay.layout.units.items() if k not in gone}
                units[option.path] = configured
                writes.update(dict.fromkeys(gone))
                member = unit.members[0]
                if member in snapshot.layout.fuzzy:
                    writes[snapshot.layout.fuzzy[member]["path"]] = None
                fuzzy = {k: v for k, v in overlay.layout.fuzzy.items() if k != member}
                writes["layout.toml"] = layout.dump_map(replace(overlay.layout, units=units, fuzzy=fuzzy))
                overlay = layout.overlay(snapshot, writes)
                landed = types.landed(overlay, configured, view.get(overlay, configured, first, recipe))
                if landed != snapshot.peek("types.toml"):
                    writes["types.toml"] = landed
                _generated(snapshot, writes)
                overlay = layout.overlay(snapshot, writes)
                group = proposed.layout.groups[unit.group]
                header = f"include/{group.segment}/{group.name}.h"
                affected = {option.path}
                if header in writes and writes[header] != snapshot.peek(header):
                    affected.update(_consumers(overlay, header, unit.group))
                before, after = [], []
                for path, data in writes.items():
                    if Path(path).suffix not in (".c", ".h"):
                        continue
                    owner = overlay.layout.units.get(path)
                    sdk = overlay.layout.groups[owner.group].sdk if owner else group.sdk
                    old = snapshot.peek(path)
                    if old is not None:
                        before.extend(policy.evaluate(snapshot, path, old.decode(), None, sdk))
                    if data is not None:
                        after.extend(policy.evaluate(overlay, path, data.decode(), None, sdk))
                blocking, debt = policy.scope(before, after, writes)
                if blocking:
                    last = blocking
                    continue
                result.append(_plan("publish", snapshot, writes, tuple(sorted(affected)), debt,
                                    f"publish {unit.members[0]} ({unit.path})"))
            except Refusal as error:  # one option failing outright leaves the others to try
                last = error.findings
        if not result:
            raise Refusal(*last or (Finding("land.request", "No publication layout option is available.",
                                            path=unit.path),))
        return result
def _learned(snapshot, unit, member, note, plan):
    attempts = [a for a in crack.history(snapshot.config, member) if a.outcome != "exact"]
    facts = attempts[-1].symptoms if attempts else {}
    group = snapshot.layout.groups.get(unit.group)
    row = {"id": f"learned-{member}", "subsystem": group.subsystem if group else "unknown",
           "detect": "", "match": {k: ({"eq": v} if isinstance(v, bool) else {"gte": v})
                                   for k, v in facts.items() if k != "score" and v not in (False, 0)},
           "technique": note, "example": "", "scope": "ordinary", "qualifier_effect": "none"}
    configuration.validate("hint", row, "hints.jsonl")
    writes = dict(plan.writes)
    old = layout.overlay(snapshot, writes).peek("hints.jsonl") or b""
    writes["hints.jsonl"] = old + (b"\n" if old and not old.endswith(b"\n") else b"") + (
        json.dumps(row, sort_keys=True) + "\n").encode()
    return _plan(plan.operation, snapshot, writes, plan.affected, plan.debt, plan.message)
def land(config: Config, submission: Submission) -> Receipt:
    with effort.stage("publish.land"):
        snapshot = layout.capture(config)
        member = submission.member
        unit = layout.unit_of(snapshot, member)
        if unit is None and submission.operation in ("repair", "withdraw"):
            raise Refusal(Finding("land.request", "The requested unit does not exist.", unit=member))
        if submission.operation == "withdraw":
            return withdraw(config, unit.path, Finding(
                "repair.not_exact", submission.note or "no mechanical repair exists",
                unit=member, missing=("mechanical repair",)))
        inbox = config.project.root / submission.source
        if submission.operation == "publish":
            kinds = configuration.load_resource("units.toml")["kind"]
            if unit is not None and kinds[unit.kind]["decompiled"] and snapshot.read(unit.path) == inbox.read_bytes():
                raise Refusal(Finding("land.duplicate", f"{member} is already landed in {unit.path}", unit=member))
            request = {"file": str(inbox), "function": submission.function or member,
                       "overrides": submission.overrides, "note": submission.note}
            unit, proposed, _ = admit(snapshot, request)
            for plan in plans(snapshot, unit, proposed):
                proofs, gaps = _proofs(layout.overlay(snapshot, plan.writes), plan.affected)
                gaps = _regressions(snapshot, gaps, unit.members)
                if not gaps:
                    if submission.note:
                        plan = _learned(snapshot, unit, member, submission.note, plan)
                    commit = journal.apply(config, plan, snapshot.commit)
                    return Receipt("publish", commit, plan.digest, proofs, len(plan.debt), effort.invocation())
            raise Refusal(*gaps)
        if submission.operation == "fuzzy":
            unit, bound = compare.bind(snapshot, inbox, member)
            group = bound.layout.groups[unit.group]
            text = bound.read(unit.path).decode()
            _blocking(policy.evaluate(bound, unit.path, text, None, group.sdk))
            recipe = recipes.resolve(config, unit, submission.overrides)
            holders = compare.holders(bound, unit)
            views = view.all_versions(bound, unit, recipe, holders)
            _blocking(policy.evaluate(bound, unit.path, text, views[holders[0]], group.sdk))
            proofs = compare.measure(bound, unit, submission.overrides, None)
            failed = tuple(p for p in proofs if any(p.symptoms.get(k)
                           for k in ("compile_failed", "unresolved", "no_measurement")))
            if failed:
                raise Refusal(Finding("land.not_exact", "The fuzzy candidate could not be measured.", unit=member,
                                      missing=tuple(m for p in failed for m in p.missing),
                                      symptoms=symptoms.merge([p.symptoms for p in failed])))
            score = min(p.score for p in proofs)
            old = min(snapshot.layout.fuzzy[member]["scores"].values()) if member in snapshot.layout.fuzzy else 0.0
            if score - old < configuration.load_resource("flow.toml")["fuzzy"]["min_gain"]:
                raise Refusal(Finding("fuzzy.no_gain", "The fuzzy candidate has insufficient gain.", unit=member))
            path = f"src/fuzzy/{store.stem(member).removesuffix('.c')}.c"
            fuzzy = dict(snapshot.layout.fuzzy)
            fuzzy[member] = {"path": path, "scores": {p.version: p.score for p in proofs}}
            writes = {path: inbox.read_bytes(), "layout.toml": layout.dump_map(replace(snapshot.layout, fuzzy=fuzzy))}
            _generated(snapshot, writes)
            plan = _plan("fuzzy", snapshot, writes, (), (), f"fuzzy {member} {score * 100:.1f}%")
        elif submission.operation == "repair":
            writes = {unit.path: inbox.read_bytes()}
            overlay = layout.overlay(snapshot, writes)
            affected = {unit.path}
            if Path(unit.path).suffix == ".h":
                affected.update(_consumers(overlay, unit.path))
            proofs, gaps = _proofs(overlay, tuple(sorted(affected)))
            if gaps:
                return withdraw(config, unit.path, Finding(
                    "repair.not_exact", gaps[0].reason, unit=unit.path, missing=gaps[0].missing))
            plan = _plan("repair", snapshot, writes, tuple(sorted(affected)), (), f"repair: {unit.path}")
        else:
            raise Refusal(Finding("land.request", "Unknown submission operation.", unit=member))
        commit = journal.apply(config, plan, snapshot.commit)
        return Receipt(submission.operation, commit, plan.digest, proofs, 0, effort.invocation())
def _restore_rows(node, placements, compiled, assembly):
    if isinstance(node, dict):
        for value in node.values():
            _restore_rows(value, placements, compiled, assembly)
    elif isinstance(node, list):
        restored = []
        for index, row in enumerate(node):
            if isinstance(row, list) and len(row) >= 2 and row[1] == compiled and isinstance(row[0], int):
                end = next((r[0] for r in node[index + 1:]
                            if isinstance(r, list) and r and isinstance(r[0], int)), float("inf"))
                matches = [(offset, name) for offset, name in placements if row[0] <= offset < end]
                if matches:
                    restored.extend([offset, assembly, name] for offset, name in sorted(matches))
                    continue
            _restore_rows(row, placements, compiled, assembly)
            restored.append(row)
        node[:] = restored
def withdraw(config: Config, unit_path: str, cause: Finding) -> Receipt:
    with effort.stage("publish.withdraw"):
        snapshot = layout.capture(config)
        unit = snapshot.layout.units.get(unit_path)
        if unit is None:
            raise Refusal(Finding("land.request", "The requested unit does not exist.", path=unit_path))
        source = snapshot.read(unit_path)
        archive = config.project.root / ".unbake" / "withdrawn" / f"{sha256(source).hexdigest()}.c"
        archive.parent.mkdir(parents=True, exist_ok=True)
        with store.work(config) as work:
            temporary = work / "withdrawn.c"
            temporary.write_bytes(source)
            temporary.replace(archive)
        writes = {unit_path: None}
        units = dict(snapshot.layout.units)
        del units[unit_path]
        writes["layout.toml"] = layout.dump_map(replace(snapshot.layout, units=units))
        kinds = configuration.load_resource("units.toml")["kind"]
        assembly = next(k for k, spec in kinds.items() if spec["phases"] == ["assemble", "link"])
        holders = compare.holders(snapshot, unit)
        for version in holders:
            path = snapshot.versions[version].split
            document = yaml.safe_load(snapshot.read(path))
            placements = [(p.rom_start, name) for name in unit.members
                          for p in snapshot.layout.members[name].placements
                          if p.version == version and p.section == ".text"]
            _restore_rows(document, placements, unit.kind, assembly)
            writes[path] = yaml.safe_dump(document, sort_keys=False).encode()
        _generated(snapshot, writes)
        plan = _plan("withdraw", snapshot, writes, (), (), f"withdraw {unit_path}: {cause.key}")
        commit = journal.apply(config, plan, snapshot.commit)
        for version in holders:
            build.extract(config, version)
        return Receipt("withdraw", commit, plan.digest, (), 0, effort.invocation())
