"""Measure every candidate before pinning an explicitly tied region."""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from unbake.decomp.candidate_ranking import measured_candidate_rank
from unbake.project import compiler_ties, toolchain
from unbake.project.config import Held, Policy, Project

if TYPE_CHECKING:
    from unbake.decomp.trial import Trial


def resolve(
    project: Project, policy: Policy, source: Path, work: Path, pinned: dict[str, tuple[Path, Path]]
) -> Project:
    from unbake.decomp.trial import try_draft
    from unbake.decomp.trial_target import owning_versions

    ref = compiler_ties.reference(project, source)
    if ref is None:
        return project
    if set(pinned) != set(owning_versions(project, source.stem, None)):
        raise Held("try", f"compiler.tie_versions: {ref}: first try must compare every containing version")
    config_hash = hashlib.sha256((project.root / "config.toml").read_bytes()).hexdigest()
    evidence: dict[str, Any] = {
        "function": source.stem,
        "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "config_sha256": config_hash,
        "targets": {v: hashlib.sha256(t.read_bytes()).hexdigest() for v, (_, t) in pinned.items()},
        "candidates": {},
    }
    results: dict[str, Trial] = {}
    for ident in project.compiler_ties[ref]:
        spec = toolchain.specification(ident)
        row: dict[str, Any] = {"compiler_pins": spec.pins, "cflags": list(project.compilers[ident].cflags)}
        evidence["candidates"][ident] = row
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                result = try_draft(compiler_ties.candidate(project, ref, ident), policy, source, work, pinned=pinned)
        except Held as error:
            row["error"] = error.reason
            print(f"compiler candidate {ident}: compile refused: {error.reason}")
            continue
        results[ident] = result
        row["rank"] = list(measured_candidate_rank(result.compares))
        row["versions"] = {
            v: {"identical": c.identical, "of": c.of, "typed": c.typed, "match_percent": c.match_percent}
            for v, c in result.compares.items()
        }
        print(f"compiler candidate {ident}: rank={row['rank']}")
    receipt = work / "compiler-candidates.json"
    receipt.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n")
    if len(results) != len(project.compiler_ties[ref]):
        raise Held("try", f"compiler.tie_incomplete: {ref}: every candidate must compile; evidence {receipt}")
    if hashlib.sha256(source.read_bytes()).hexdigest() != evidence["source_sha256"] or any(
        hashlib.sha256(target.read_bytes()).hexdigest() != evidence["targets"][version]
        for version, (_, target) in pinned.items()
    ):
        raise Held("try", "compiler.tie_stale: source or target changed during candidate comparisons")
    best = min(measured_candidate_rank(result.compares) for result in results.values())
    winners = [ident for ident, result in results.items() if measured_candidate_rank(result.compares) == best]
    if len(winners) != 1:
        raise Held("try", f"compiler.tie_equivalent: {ref}: candidates still equivalent; evidence {receipt}")
    winner = winners[0]
    evidence["selected"] = winner
    evidence["reason"] = (
        "unique exact reproduction" if results[winner].identical_everywhere else "strictly better measured rank"
    )
    evidence["generations"] = {v: str(g) for v, (g, _) in pinned.items()}
    receipt.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n")
    resolved = compiler_ties.pin(project, ref, winner, evidence, config_hash)
    print(f"compiler pin {ref}: {winner}; {evidence['reason']}; config.toml records candidate evidence")
    return resolved
