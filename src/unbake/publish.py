"""Admission, submission landing and withdrawal."""
import posixpath
import re
from collections import defaultdict
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

from unbake import (
    compare,
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
        jobs = []
        # their data is proved with their code
        units = ownership.derive_many(snapshot, [snapshot.layout.units[path] for path in affected])
        for own, unit in units:
            recipe = recipes.resolve(snapshot.config, unit, unit.options)
            for version in compare.holders(own, unit):
                jobs.append((own, unit, version, recipe, work / f"{len(jobs)}"))
        batches = pool.map(snapshot.config, "publish.prove", native.prove_args, jobs)
        proofs = tuple(p for batch in batches for p in batch)
        gaps = tuple(f for own, unit in units for f in compare.gaps(own, unit, proofs))
        return proofs, gaps
def _regressions(snapshot: Snapshot, gaps: tuple[Finding, ...], own: Sequence[str],
                 candidate: str) -> tuple[Finding, ...]:
    """Another member's gap blocks only in a version where HEAD proves it exact, and is then named for what it is: the
    candidate broke a landed file."""
    heads = {g.unit: layout.unit_of(snapshot, g.unit) for g in gaps if g.unit not in own}
    paths = sorted({u.path for u in heads.values() if u is not None})
    exact = {(p.member, p.version) for p in _proofs(snapshot, paths)[0] if p.exact} if paths else set()
    kept = []
    for g in gaps:
        head = heads.get(g.unit)
        if g.unit in own or head is None:
            kept.append(g)
        elif any((g.unit, v) in exact for v in g.versions):
            kept.append(Finding(
                "land.breaks_dependent", f"{candidate} breaks {head.path}: {g.missing[0].splitlines()[0]}",
                path=head.path, unit=g.unit, versions=g.versions, missing=g.missing, symptoms=g.symptoms,
                action=f"make the declaration agree with {head.path}, or change both"))
    return tuple(kept)
def _consumers(snapshot: Snapshot, changed: Sequence[str]) -> tuple[str, ...]:
    """Every unit, of any group, that includes a changed header directly or through other headers. A quote include
    names a file beside the including one, else under include/ or src/; taking every one that exists
    over-approximates."""
    if not changed:
        return ()
    known = {*headers.sources(snapshot), *snapshot.layout.units, *changed}
    included_by = defaultdict(set)
    for path in known:
        for name in re.findall(rb'^\s*#\s*include\s*"([^"]+)"', snapshot.peek(path) or b"", re.M):
            for base in (posixpath.dirname(path), "include", "src"):
                if (target := posixpath.normpath(posixpath.join(base, name.decode(errors="replace")))) in known:
                    included_by[target].add(path)
    reached, todo = set(), list(changed)
    while todo:
        new = included_by[todo.pop()] - reached
        reached |= new
        todo.extend(new)
    return tuple(path for path in reached if path in snapshot.layout.units)
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
def plans(snapshot: Snapshot, unit: UnitSpec, proposed: Snapshot, extras: Mapping[str, bytes]) -> Iterator[Plan]:
    """The publication layout options, each planned only when the caller asks for it: the first option that proves
    lands, so the others cost nothing. `extras` are a change set's other files, written in every plan."""
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
        options = [(owner, {})] if again else layout.unit_options(snapshot, unit.members[0], folded[unit.path])
    return _option_plans(snapshot, unit, proposed, (folded, again, first, recipe, extras), options)
def _option_plans(snapshot: Snapshot, unit: UnitSpec, proposed: Snapshot, shared: tuple[Any, ...],
                  options: Sequence[tuple[UnitSpec, dict[str, bytes | None]]]) -> Iterator[Plan]:
    produced, last = False, ()
    for option, writes in options:
        try:
            with effort.stage("publish.plans"):
                made = _option_plan(snapshot, unit, proposed, shared, option, writes)
        except Refusal as error:  # one option failing outright leaves the others to try
            last = error.findings
            continue
        if isinstance(made, Plan):
            produced = True
            yield made
        else:
            last = made
    if not produced:
        raise Refusal(*last or (Finding("land.request", "No publication layout option is available.",
                                        path=unit.path),))
def _option_plan(snapshot: Snapshot, unit: UnitSpec, proposed: Snapshot, shared: tuple[Any, ...], option: UnitSpec,
                 writes: dict[str, bytes | None]) -> Plan | tuple[Finding, ...]:
    folded, again, first, recipe, extras = shared
    writes = {**extras, **writes}
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
    affected = {option.path, *extras.keys() & overlay.layout.units.keys()}
    edited = [p for p, data in writes.items() if p.endswith(".h") and data != snapshot.peek(p)]
    affected.update(_consumers(overlay, edited))
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
        return blocking
    return _plan("publish", snapshot, writes, tuple(sorted(affected)), debt,
                 f"publish {unit.members[0]} ({unit.path})")

def land(config: Config, submission: Submission) -> Receipt:
    with effort.stage("publish.land"):
        snapshot = layout.capture(config)
        member = submission.member
        unit = layout.unit_of(snapshot, member)
        inbox = config.project.root / submission.source
        if submission.operation == "publish":
            kinds = configuration.load_resource("units.toml")["kind"]
            extras = {path: (config.project.root / copy).read_bytes() for path, copy in submission.extras.items()}
            if (unit is not None and kinds[unit.kind]["decompiled"] and snapshot.read(unit.path) == inbox.read_bytes()
                    and not extras):
                raise Refusal(Finding("land.duplicate", f"{member} is already landed in {unit.path}", unit=member))
            request = {"file": str(inbox), "function": submission.function or member,
                       "overrides": submission.overrides, "note": submission.note}
            unit, proposed, _ = admit(layout.overlay(snapshot, extras) if extras else snapshot, request)
            touched = (*unit.members, *(m for p in extras if p in snapshot.layout.units
                                        for m in snapshot.layout.units[p].members))
            for plan in plans(snapshot, unit, proposed, extras):
                proofs, gaps = _proofs(layout.overlay(snapshot, plan.writes), plan.affected)
                gaps = _regressions(snapshot, gaps, touched, unit.path)
                if not gaps:
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
        else:
            raise Refusal(Finding("land.request", "Unknown submission operation.", unit=member))
        commit = journal.apply(config, plan, snapshot.commit)
        return Receipt(submission.operation, commit, plan.digest, proofs, 0, effort.invocation())
