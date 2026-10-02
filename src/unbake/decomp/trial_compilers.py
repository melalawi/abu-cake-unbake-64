"""Measure other compilers only when the item's configured compiler is not exact."""

from __future__ import annotations

import contextlib
import hashlib
import io
from pathlib import Path
from typing import TYPE_CHECKING, Any

from unbake.decomp.candidate_ranking import measured_candidate_rank
from unbake.project import compiler_choice, toolchain
from unbake.project.config import Held, Policy, Project

if TYPE_CHECKING:
    from unbake.decomp.trial import Trial


def _row(project: Project, policy: Policy, ident: str) -> dict[str, Any]:
    from unbake.decomp.work import compiler_identity

    return {
        "compiler_pins": toolchain.specification(ident).pins,
        "cflags": list(project.compilers[ident].cflags),
        "inputs": compiler_identity(project, policy, ident),
    }


def _measured(row: dict[str, Any], result: Trial) -> None:
    row["rank"] = list(measured_candidate_rank(result.compares))
    row["exact"] = result.identical_everywhere
    row["versions"] = {
        v: {
            "identical": c.identical,
            "of": c.of,
            "match_percent": c.match_percent,
            "target_words": [f"0x{word:08X}" for word in c.target_words],
            "candidate_words": [f"0x{word:08X}" for word in c.candidate_words],
        }
        for v, c in result.compares.items()
    }


def resolve(
    project: Project,
    policy: Policy,
    source: Path,
    compiled: Path,
    work: Path,
    pinned: dict[str, tuple[Path, Path]],
    configured: Trial | Held,
) -> tuple[Project, dict[str, Any], Trial]:
    """Keep an exact configured result; otherwise rank every other configured compiler by bytes.

    A candidate that fails to compile, the configured one included, is eliminated with its
    reason; a compile failure holds the trial only when every candidate fails.
    """
    from unbake.decomp.trial import try_draft
    from unbake.decomp.trial_target import owning_versions

    function = source.stem
    complete = set(pinned) == set(owning_versions(project, function, None))
    if isinstance(configured, Held):
        if not complete:
            raise configured
    elif configured.identical_everywhere or not complete:
        return project, {}, configured
    own = project.compiler_reference(function)
    evidence: dict[str, Any] = {
        "function": function,
        "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "configured": own,
        "targets": {v: hashlib.sha256(t.read_bytes()).hexdigest() for v, (_, t) in pinned.items()},
        "candidates": {},
        "eliminated": {},
    }
    results: dict[str, Trial] = {}
    if isinstance(configured, Held):
        evidence["eliminated"][own] = configured.reason
        print(f"compiler candidate {own}: eliminated: {configured.reason.splitlines()[0]}")
    else:
        evidence["candidates"][own] = _row(project, policy, own)
        _measured(evidence["candidates"][own], configured)
        results[own] = configured
    for ident in compiler_choice.alternatives(project, function):
        candidate = compiler_choice.selected(project, function, ident)
        row = _row(candidate, policy, ident)
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                result = try_draft(candidate, policy, compiled, work, pinned=pinned, function=function)
        except Held as error:
            evidence["eliminated"][ident] = error.reason
            print(f"compiler candidate {ident}: eliminated: {error.reason.splitlines()[0]}")
            continue
        _measured(row, result)
        evidence["candidates"][ident] = row
        results[ident] = result
        print(
            f"compiler candidate {ident}: rank={row['rank']}; "
            + ("exact reproduction" if result.identical_everywhere else "not exact")
        )
    if hashlib.sha256(source.read_bytes()).hexdigest() != evidence["source_sha256"] or any(
        hashlib.sha256(target.read_bytes()).hexdigest() != evidence["targets"][version]
        for version, (_, target) in pinned.items()
    ):
        raise Held("try", f"trial.inputs_changed: {source}: source or target changed during compiler comparisons")
    if not results:
        reasons = "; ".join(f"{ident}: {reason.splitlines()[0]}" for ident, reason in evidence["eliminated"].items())
        raise Held("try", f"compiler.no_candidate: {source}: every configured compiler failed to compile: {reasons}")
    exact = [ident for ident, result in results.items() if result.identical_everywhere]
    evidence["exact_candidates"] = exact
    if exact:
        winner, rule = compiler_choice.build_choice(project, function, exact)
        evidence["reason"] = "equivalent" if len(exact) > 1 else "unique exact reproduction"
        evidence["build_rule"] = rule
    else:
        best = min(measured_candidate_rank(result.compares) for result in results.values())
        ranked = [ident for ident, result in results.items() if measured_candidate_rank(result.compares) == best]
        winner = own if own in ranked else ranked[0]
        evidence["reason"] = "configured compiler" if winner == own else "strictly better measured rank"
    evidence["selected"] = winner
    if winner == own:
        return project, evidence, results[own]
    print(f"compiler selected {function}: {winner}; {evidence['reason']}")
    from unbake.decomp.trial import render

    print(render(results[winner]))
    return compiler_choice.selected(project, function, winner), evidence, results[winner]
