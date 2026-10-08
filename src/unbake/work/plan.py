"""Which functions to work on next: candidates from the inventory, ranked by expected bytes per minute."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from unbake.cache import Cache
from unbake.config import Host, Project
from unbake.cycle import rank
from unbake.decomp import checks, exclusions
from unbake.layout import boundary, split
from unbake.work import attempts, inventory, shape

if TYPE_CHECKING:
    from unbake.compilers.families.mips import Shape
    from unbake.layout.subsystems import Snapshot
    from unbake.project.flow import ProviderRecord

from unbake.compilers.registry import specification


@dataclass(frozen=True)
class Action:
    words: tuple[str, ...] | None
    reason: str
    function: str | None


Verdict = tuple["Shape", "Shape", tuple[tuple[bytes, int, boundary.Boundary], ...]]


def _drafter(verdict: Verdict) -> bool:
    target, emitted, bodies = verdict
    return all(shape.classify(body, address, target, emitted, owned)[0] == "drafter" for body, address, owned in bodies)


def _placement(project: Project, item: split.Function, data: bytes, target: Shape) -> boundary.Boundary:
    """Selection consumes the existing boundary and table-provider proof for this placement."""
    from unbake import cache, inputs
    from unbake.layout import planner, rodata_owners

    configured = project.version(item.version)

    def load() -> tuple[list[split.Function], bytes, list[ProviderRecord]]:
        rows = split.functions(project, item.version)
        image = configured.baserom.read_bytes()
        pools = rodata_owners.mapped_spans(project, item.version, include_data=True)
        return rows, image, planner.carve(image, rows, pools)

    rows, image, constants = cache.memo(
        "boundary.placements",
        (
            configured.baserom,
            inputs.digest(configured.baserom, algorithm="sha256", reuse=cache.configured()),
            configured.split,
            inputs.digest(configured.split, algorithm="sha256", reuse=cache.configured()),
            configured.symbols,
            inputs.digest(configured.symbols, algorithm="sha256", reuse=cache.configured()),
            project.resident_mappings.get(item.version, ()),
        ),
        load,
        size=cache.memory_size,
        copy_out=cache.clone,
    )
    return boundary.evidence(
        {item.start + index * 4: word for index, word in enumerate(shape.words_of(data))},
        item.start,
        item.end,
        item.address - item.start,
        {"layout-placement"},
        {row.address - (item.address - item.start) for row in rows},
        target,
        planner.table_edges(image, item, constants),
    )


def candidates(project: Project, host: Host, *, selected: frozenset[str] | None = None) -> list[rank.Candidate]:
    """Units not yet exact and clean: unmatched functions m2c can draft (complete bodies, one name in every
    version, not excluded), and published units that break a source rule (drafted from their src/ text).
    The word rules run in the worker pool, in chunks of groups."""
    from unbake import pool

    excluded = exclusions.load(project)
    _, functions, bodies = inventory.inventory(project)
    carry = set(attempts.ledger(project).summaries())
    history = attempts.ledger(project).summaries()
    shapes, emitted = shape.configured(project)
    picked: list[tuple[split.Function, tuple[split.Function, ...]]] = []
    verdicts: list[Verdict] = []
    for group in inventory.groups(functions, bodies):
        items = tuple(group)
        canonical = min(items, key=lambda item: project.versions.index(item.version))
        aliases = {name for item in items for name in (item.name, *item.aliases)}
        if selected is not None and not aliases & selected:
            continue
        if aliases & excluded or all(item.kind == "c" for item in items):
            continue
        if any(item.name != canonical.name for item in items):
            continue
        compiler = project.compiler_for(canonical.name)
        if not specification(compiler.id).m2c:
            continue
        target = shapes.get(canonical.name, shapes[compiler.id])
        picked.append((canonical, tuple(items)))
        verdicts.append(
            (
                target,
                emitted,
                tuple(
                    (
                        bodies[item.version, item.name],
                        item.address,
                        _placement(project, item, bodies[item.version, item.name], target),
                    )
                    for item in items
                ),
            )
        )
    drafters = pool.run(host, _drafter, verdicts)
    result = []
    for (canonical, items), drafter in zip(picked, drafters, strict=True):
        if not drafter:
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
    sources = [
        source
        for source in sorted(project.src.glob("*.c"))
        if source.stem in published and (selected is None or source.stem in selected)
    ]
    for source in sorted(
        {project.root / row.path for row in checks.findings(project, sources, Cache(project.cache)).unmarked}
    ):
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
    shapes, emitted = shape.configured(project)
    result = []
    for items in inventory.groups(functions, bodies):
        names = {item.name for item in items}
        if len(names) != 1 or any(item.kind != "asm" for item in items):
            continue
        target = shapes.get(items[0].name, shapes[project.compiler_for(items[0].name).id])
        routes = [
            shape.classify(
                bodies[item.version, item.name],
                item.address,
                target,
                emitted,
                _placement(project, item, bodies[item.version, item.name], target),
            )
            for item in items
        ]
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


def refusal(project: Project, name: str) -> str:
    """The one reason a named function is not a candidate: published and clean, unknown, or not draftable."""
    if name in _published_rows(project):
        return "published and clean"
    if name in exclusions.load(project):
        return f"excluded by {exclusions.MANIFEST}"
    _, functions, bodies = inventory.inventory(project)
    if not any(name in (item.name, *item.aliases) for item in functions):
        return "unknown function"
    shapes, emitted = shape.configured(project)
    reasons = []
    for item in functions:
        if name not in (item.name, *item.aliases):
            continue
        compiler = project.compiler_for(item.name)
        if not specification(compiler.id).m2c:
            reasons.append(f"{item.version} {item.name}: compiler {compiler.id} has no drafter")
            continue
        target = shapes.get(item.name, shapes[compiler.id])
        route, cause = shape.classify(
            bodies[item.version, item.name],
            item.address,
            target,
            emitted,
            _placement(project, item, bodies[item.version, item.name], target),
        )
        reasons.append(f"{item.version} {item.name} @0x{item.address:X}: {route}: {cause}")
    return "; ".join(reasons)


def history(project: Project) -> list[rank.History]:
    return [
        rank.History(function, summary.bytes, summary.exact, summary.minutes)
        for function, summary in attempts.ledger(project).summaries().items()
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
            found = attempts.ledger(project).history(row.function)
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


@dataclass(frozen=True)
class Cohort:
    """A manual assignment proposal. It neither claims paths nor dispatches work."""

    id: str
    snapshot_key: str
    family_ids: tuple[str, ...]
    candidate_ids: tuple[str, ...]
    provider_ids: tuple[str, ...]
    write_paths: tuple[str, ...]
    read_dependencies: tuple[str, ...]
    affected_consumers: tuple[str, ...]
    required_versions: tuple[str, ...]
    estimated_new_bytes: int
    primary_label: str | None
    state: str
    ownership_conflicts: tuple[str, ...]
    omitted_candidates: tuple[str, ...]
    basis: str = "existing candidate rank; delivered elapsed observations unavailable"
    estimated_wall_seconds: float | None = None


def cohorts(
    candidates: list[rank.Candidate],
    snapshot: Snapshot,
    claims: dict[str, str] | None = None,
    *,
    owner: str | None = None,
) -> tuple[Cohort, ...]:
    """Overlapping provider/module families in existing rank order; unknown rows remain selectable.

    Candidates are already admitted by the existing route. A family may recommend other
    consumers, but never adds them to an explicit function selection or assigns their writer.
    """
    from unbake import cache

    by_name = {row.function: row for row in candidates}
    entities = {row.id: row for row in snapshot.entities}
    memberships = {row.entity_id: row for row in snapshot.memberships}
    positions = {row.function: index for index, row in enumerate(candidates)}
    families = list(snapshot.families)
    covered = {key for row in families for key in row.members}
    for entity in snapshot.entities:
        if entity.kind == "function" and entity.id not in covered and entity.name in by_name:
            from unbake.layout.subsystems import Family

            families.append(Family("single:" + entity.id, entity.id, (entity.id,), "ungrouped function"))
    proposals = []
    for family in families:
        members = [entities[key] for key in family.members]
        picked = sorted(
            {row.name for row in members if row.kind == "function" and row.name in by_name}, key=positions.__getitem__
        )
        producer = [row for row in members if row.kind in {"data", "resource"}]
        if not picked and not producer:
            continue
        providers = tuple(sorted(row.id for row in members if row.kind == "provider"))
        paths = tuple(
            sorted(
                {row.path for row in members if row.path and row.kind in {"provider", "function", "data", "resource"}}
            )
        )
        conflicts = tuple(
            sorted(
                f"{path}: owned by {writer}"
                for path, writer in (claims or {}).items()
                if path in paths and (owner is None or writer != owner)
            )
        )
        support = [memberships[row.id] for row in members if row.id in memberships and row.kind != "provider"]
        labels = {row.primary for row in support if row.primary}
        state = (
            "mixed"
            if len(labels) > 1 or any(row.state == "mixed" for row in support)
            else ("assigned" if labels else "unknown")
        )
        proposals.append(
            Cohort(
                "cohort:" + cache.key(family.id),
                snapshot.evidence_key,
                (family.id,),
                tuple(picked),
                providers,
                paths,
                providers,
                tuple(sorted(row.id for row in members if row.kind == "function")),
                tuple(sorted({row.version for row in members if row.version})),
                sum(by_name[name].bytes for name in picked),
                next(iter(labels)) if len(labels) == 1 and state == "assigned" else None,
                state,
                conflicts,
                tuple(sorted({row.name for row in members if row.kind == "function" and row.name not in by_name})),
            )
        )
    return tuple(
        sorted(
            proposals,
            key=lambda row: (
                min((positions[name] for name in row.candidate_ids), default=len(positions)),
                row.id,
            ),
        )
    )
