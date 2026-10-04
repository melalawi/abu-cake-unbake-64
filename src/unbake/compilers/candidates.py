"""When the unit's configured compiler is not exact, measure the other configured compilers and pick by rank."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from unbake.compilers import choice
from unbake.compilers.ranking import measured_candidate_rank
from unbake.config import Held, Host, Project


@dataclass(frozen=True)
class Choice:
    compiler: str
    reason: str
    exact: tuple[str, ...]
    eliminated: dict[str, str] = field(default_factory=dict)


def resolve(project: Project, host: Host, file: Path, configured: object) -> tuple[Choice, object]:
    """(choice, measured result) where configured is the configured compiler's Compared result or its Held."""
    from unbake.work.compare import Compared, measure

    function = file.stem
    own = project.compiler_reference(function)
    if isinstance(configured, Compared) and configured.identical_everywhere:
        return Choice(own, "configured compiler is exact", (own,)), configured
    results: dict[str, Compared] = {}
    eliminated: dict[str, str] = {}
    if isinstance(configured, Held):
        eliminated[own] = configured.reason.splitlines()[0]
    elif isinstance(configured, Compared):
        results[own] = configured
    for ident in choice.alternatives(project, function):
        try:
            results[ident] = measure(choice.selected(project, function, ident), host, file)
        except Held as error:
            eliminated[ident] = error.reason.splitlines()[0]
    if not results:
        detail = "; ".join(f"{ident}: {reason}" for ident, reason in eliminated.items())
        raise Held("compare", f"compiler.no_candidate: {file}: every configured compiler failed: {detail}")
    exact = tuple(ident for ident, result in results.items() if result.identical_everywhere)
    if exact:
        winner, rule = choice.build_choice(project, function, list(exact))
        return Choice(winner, rule, exact, eliminated), results[winner]
    best = min(measured_candidate_rank(result.compares) for result in results.values())
    ranked = [ident for ident, result in results.items() if measured_candidate_rank(result.compares) == best]
    winner = own if own in ranked else ranked[0]
    reason = "configured compiler" if winner == own else "strictly better measured rank"
    return Choice(winner, reason, (), eliminated), results[winner]
