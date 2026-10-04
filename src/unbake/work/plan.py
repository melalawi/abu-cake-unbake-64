"""Which functions to work on next: candidates from the inventory, ranked by expected bytes per minute."""

from __future__ import annotations

from dataclasses import dataclass

from unbake.config import Host, Project
from unbake.cycle import rank
from unbake.decomp import exclusions
from unbake.work import attempts, inventory

M2C_KINDS = ("sn64", "ido")


@dataclass(frozen=True)
class Action:
    words: tuple[str, ...] | None
    reason: str
    function: str | None


def candidates(project: Project) -> list[rank.Candidate]:
    """Unmatched functions m2c can draft: complete bodies, one name in every version, not excluded."""
    excluded = exclusions.load(project)
    _, functions, bodies = inventory.inventory(project)
    carry = {name for name in attempts.functions(project) if attempts.path(project, name).is_file()}
    history = attempts.summaries(project)
    result = []
    for items in inventory.groups(functions, bodies):
        if all(item.kind == "c" for item in items):
            continue
        canonical = min(items, key=lambda item: project.versions.index(item.version))
        aliases = {name for item in items for name in (item.name, *item.aliases)}
        if aliases & excluded or any(item.kind == "c" for item in items):
            continue
        if any(inventory.classify(bodies[item.version, item.name])[0] != "drafter" for item in items):
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
    return result


def history(project: Project) -> list[rank.History]:
    return [
        rank.History(function, summary.bytes, summary.exact, summary.minutes)
        for function, summary in attempts.summaries(project).items()
    ]


def ranked(project: Project, host: Host) -> list[rank.Candidate]:
    return rank.rank(
        candidates(project),
        history(project),
        min_bytes=host.cycle_min_bytes,
        max_bytes=host.cycle_max_bytes,
        min_history=host.cycle_min_history,
    )


def next_action(project: Project, host: Host, *, undrafted: bool) -> Action:
    """Continue existing work first (unless undrafted), then draft the best-ranked new function."""
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
    for row in order:
        if row.carryover:
            continue
        reason = f"{row.function}: {row.bytes} bytes in {', '.join(row.versions)}"
        return Action(("draft", row.function), reason, row.function)
    return Action(None, "no unmatched function is ready to draft", None)
