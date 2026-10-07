"""When the unit's configured compiler is not exact, measure the other configured compilers and pick by rank."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from unbake.compilers import choice
from unbake.compilers.ranking import measured_candidate_rank
from unbake.config import Held, Host, Project
from unbake.process import Fault, capture
from unbake.process import named as cause_named


@dataclass(frozen=True)
class Choice:
    compiler: str
    reason: str
    exact: tuple[str, ...]
    eliminated: dict[str, str] = field(default_factory=dict)


def resolve(
    project: Project, host: Host, file: Path, configured: object, *, required_versions: tuple[str, ...] | None = None
) -> tuple[Choice, object]:
    """(choice, measured result) where configured is the configured compiler's Compared result or its Held."""
    from unbake.work.compare import Compared, measure

    function = file.stem
    own = project.compiler_reference(function)

    def required(result: Compared) -> bool:
        return (
            result.identical_everywhere
            if required_versions is None
            else all(version in result.compares and result.compares[version].exact for version in required_versions)
        )

    def rank(result: Compared) -> tuple[bool, int, int, float]:
        return measured_candidate_rank(
            {v: c for v, c in result.compares.items() if required_versions is None or v in required_versions}
        )

    if isinstance(configured, Compared) and required(configured):
        return Choice(own, "configured compiler is exact", (own,)), configured
    results: dict[str, Compared] = {}
    eliminated: dict[str, str] = {}
    faults = {}
    if isinstance(configured, Held) and configured.key == "link.undefined":
        raise configured  # a symbol no version provides fails the same under every compiler
    if isinstance(configured, Held):
        eliminated[own] = configured.reason.splitlines()[0]
        faults[own] = capture(
            configured,
            cause=cause_named(
                "compilers.candidates.unexpected", str(configured), owner="compilers.candidates", stage="compilers"
            ),
        ).document()
    elif isinstance(configured, Compared):
        results[own] = configured
    for ident in choice.alternatives(project, function):
        try:
            view = choice.selected(project, function, ident)
            results[ident] = (
                measure(view, host, file)
                if required_versions is None
                else measure(view, host, file, retain_link_faults=True)
            )
        except Held as error:
            eliminated[ident] = error.reason.splitlines()[0]
            faults[ident] = capture(
                error,
                cause=cause_named(
                    "compilers.candidates.unexpected", str(error), owner="compilers.candidates", stage="compilers"
                ),
            ).document()
    if not results:
        detail = "; ".join(f"{ident}: {reason}" for ident, reason in eliminated.items())
        first = Fault.read(next(iter(faults.values())))
        raise Held(
            first.framed("compilers.candidates", "compare", detail, {"compilers": faults}), data={"compilers": faults}
        )
    best = min(rank(result) for result in results.values())
    ranked = [ident for ident, result in results.items() if rank(result) == best]
    winner = own if own in ranked else ranked[0]
    reason = "configured compiler" if winner == own else "strictly better measured rank"
    return Choice(winner, reason, (), eliminated), results[winner]
