"""Assign the entire measured regional proposal for one explicit confirmation."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence


def assignments(
    units: Mapping[str, Sequence[str]], winners: Mapping[str, str | None], choices: Mapping[str, str]
) -> tuple[dict[str, str], list[str], str | None]:
    """Unit ambiguity is covered by regional evidence; regional ties are explicit.

    Every unit retains all containing regions. Contradictory regional winners
    remain a named conflict, and an explicit unit choice can resolve it.
    """
    resolved: dict[str, str] = {}
    unresolved = []
    for name, regions in sorted(units.items()):
        if name in choices:
            resolved[name] = choices[name]
            continue
        candidates = {winners[region] for region in regions}
        if None not in candidates and len(candidates) == 1:
            resolved[name] = str(next(iter(candidates)))
        elif candidates == {None} and choices.get("default") and all(r.endswith(":undecided") for r in regions):
            resolved[name] = choices["default"]
        elif len(candidates - {None}) > 1:
            unresolved.append(f"unit:{name}:mixed")
        else:
            unresolved.extend(region for region in regions if winners[region] is None)
    default = choices.get("default")
    if default is None and resolved:
        # All unit recipes are explicit. The fallback is displayed in the
        # proposal, covered by its digest and used only for newly named units.
        counts = Counter(resolved.values())
        default = min(counts, key=lambda ident: (-counts[ident], ident))
    if default is None:
        unresolved.append("default:missing")
    return resolved, sorted(set(unresolved)), default
