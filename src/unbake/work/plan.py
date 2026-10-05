"""Which functions to work on next: candidates from the inventory, ranked by expected bytes per minute."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from unbake.cache import Cache
from unbake.config import Host, Project
from unbake.cycle import rank
from unbake.decomp import checks, exclusions
from unbake.layout import split
from unbake.work import attempts, inventory, shape

M2C_KINDS = ("sn64", "ido")


@dataclass(frozen=True)
class Action:
    words: tuple[str, ...] | None
    reason: str
    function: str | None


def candidates(project: Project, host: Host) -> list[rank.Candidate]:
    """Units not yet exact and clean: unmatched functions m2c can draft (complete bodies, one name in every
    version, not excluded), and published units that break a source rule (drafted from their src/ text)."""
    excluded = exclusions.load(project)
    _, functions, bodies = inventory.inventory(project)
    carry = {name for name in attempts.functions(project) if attempts.path(project, name).is_file()}
    history = attempts.summaries(project)
    result = []
    before = {(item.version, item.end): item for item in functions}
    shapes = {ident: shape.for_compiler(compiler) for ident, compiler in project.compilers.items()}
    emitted = shape.emitters(project.compilers.values())
    for items in inventory.groups(functions, bodies):
        if all(item.kind == "c" for item in items):
            continue
        canonical = min(items, key=lambda item: project.versions.index(item.version))
        aliases = {name for item in items for name in (item.name, *item.aliases)}
        if aliases & excluded or any(item.kind == "c" for item in items):
            continue
        target = shapes[project.compiler_for(canonical.name).id]
        if any(
            shape.classify(bodies[item.version, item.name], item.address, target, emitted)[0] != "drafter"
            for item in items
        ):
            continue
        if any(
            (previous := before.get((item.version, item.start))) is not None
            and shape.tail(
                bodies[item.version, previous.name],
                previous.address,
                bodies[item.version, item.name],
                target,
                emitted,
            )
            for item in items
        ):
            continue
        if any(item.name != canonical.name for item in items):
            continue
        if project.compiler_for(canonical.name).kind not in M2C_KINDS:
            continue
        summary = history.get(canonical.name)
        result.append(
            rank.Candidate(
                canonical.name,
                canonical.end - canonical.start,
                tuple(item.version for item in items),
                canonical.name in carry,
                summary.best_percent if summary else None,
            )
        )
    published = _published_rows(project)
    sources = [source for source in sorted(project.src.glob("*.c")) if source.stem in published]
    for source in checks.dirty(Cache(host.cache_root), sources):
        versions, row = published[source.stem]
        summary = history.get(source.stem)
        result.append(
            rank.Candidate(
                source.stem,
                row.end - row.start,
                versions,
                source.stem in carry,
                summary.best_percent if summary else None,
            )
        )
    return result


def originals(project: Project) -> list[tuple[str, str]]:
    """Unlanded original-asm functions as (name, rule evidence): asm in every holding version, one name, and an
    original-asm route (work.shape.original) in each. They land as src/NAME.s, never as drafts."""
    _, functions, bodies = inventory.inventory(project)
    shapes = {ident: shape.for_compiler(compiler) for ident, compiler in project.compilers.items()}
    emitted = shape.emitters(project.compilers.values())
    result = []
    for items in inventory.groups(functions, bodies):
        names = {item.name for item in items}
        if len(names) != 1 or any(item.kind != "asm" for item in items):
            continue
        target = shapes[project.compiler_for(items[0].name).id]
        routes = [shape.classify(bodies[item.version, item.name], item.address, target, emitted) for item in items]
        if all(route == "original" for route, _ in routes):
            result.append((items[0].name, routes[0][1]))
    return sorted(result)


def _published_rows(project: Project) -> dict[str, tuple[tuple[str, ...], split.Function]]:
    """Each published unit (compare.published) with its holding versions and first row, from one read of every
    version's rows."""
    by_alias: dict[str, dict[str, list[split.Function]]] = {}
    for version in project.versions:
        for row in split.functions(project, version):
            for alias in row.aliases:
                by_alias.setdefault(alias, {}).setdefault(version, []).append(row)
    result = {}
    for name, rows in by_alias.items():
        holding = tuple(v for v in project.versions if any(Path(row.path).name == name for row in rows.get(v, ())))
        if holding and all(len(rows[v]) == 1 and rows[v][0].kind == "c" for v in holding):
            result[name] = (holding, rows[holding[0]][0])
    return result


def history(project: Project) -> list[rank.History]:
    return [
        rank.History(function, summary.bytes, summary.exact, summary.minutes)
        for function, summary in attempts.summaries(project).items()
    ]


def ranked(project: Project, host: Host) -> list[rank.Candidate]:
    return rank.rank(
        candidates(project, host),
        history(project),
        min_bytes=host.cycle_min_bytes,
        max_bytes=host.cycle_max_bytes,
        min_history=host.cycle_min_history,
    )


def next_action(project: Project, host: Host, *, undrafted: bool) -> Action:
    """Continue existing work first (unless undrafted), then land an original-asm function, then draft the
    best-ranked new function."""
    order = ranked(project, host)
    if not undrafted:
        for row in order:
            if not row.carryover:
                break
            file = project.work / row.function / f"{row.function}.c"
            found = attempts.read(project, row.function)
            if found and found[-1].exact:
                return Action(("publish", str(file)), f"{row.function}: exact in every version; land it", row.function)
            best = f"{row.best_percent:.2f}%" if row.best_percent is not None else "not compared"
            reason = f"{row.function}: edit {file} (best {best}), then compare it"
            return Action(("compare", str(file)), reason, row.function)
    landable = originals(project)
    if landable:
        name, evidence = landable[0]
        return Action(("publish", "--original", name), f"{name}: original asm ({evidence}); land it as .s", name)
    for row in order:
        if row.carryover:
            continue
        reason = f"{row.function}: {row.bytes} bytes in {', '.join(row.versions)}"
        return Action(("draft", row.function), reason, row.function)
    return Action(None, "no unit is ready to draft", None)
