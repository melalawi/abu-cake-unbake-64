"""Measure compiler candidates into the source's private trial receipt."""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

import toml  # type: ignore[import-untyped]

from unbake.decomp.candidate_ranking import measured_candidate_rank
from unbake.project import compiler_ties, toolchain
from unbake.project.config import Held, Policy, Project

if TYPE_CHECKING:
    from unbake.decomp.trial import Trial


def resolve(
    project: Project, policy: Policy, source: Path, work: Path, pinned: dict[str, tuple[Path, Path]]
) -> tuple[Project, dict[str, Any]]:
    from unbake.decomp.trial import try_draft
    from unbake.decomp.trial_target import owning_versions
    from unbake.decomp.work import compiler_identity, identity

    ref = compiler_ties.reference(project, source, equivalent=True)
    if ref is None:
        return project, {}
    if set(pinned) != set(owning_versions(project, source.stem, None)):
        raise Held("try", f"compiler.tie_versions: {ref}: first try must compare every containing version")
    evidence: dict[str, Any] = {
        "function": source.stem,
        "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "reference": ref,
        "candidate_set": list(project.compiler_ties[ref]),
        "targets": {v: hashlib.sha256(t.read_bytes()).hexdigest() for v, (_, t) in pinned.items()},
        "candidates": {},
    }
    selection = toml.loads((project.root / "config.toml").read_text()).get("compiler_selections", {}).get(ref, {})
    previous = json.loads(selection["evidence_json"]) if selection.get("status") == "equivalent" else {}
    evidence["excluded_evidence"] = previous.get("excluded_evidence", {})
    results: dict[str, Trial] = {}
    for ident in project.compiler_ties[ref]:
        spec = toolchain.specification(ident)
        row: dict[str, Any] = {
            "compiler_pins": spec.pins,
            "cflags": list(project.compilers[ident].cflags),
            "inputs": compiler_identity(project, policy, ident),
        }
        measured = compiler_ties.candidate(project, ref, ident, source.stem)
        before = identity(measured, source, list(pinned), pinned=pinned, policy=policy)
        row["build_inputs"] = before
        evidence["candidates"][ident] = row
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                result = try_draft(measured, policy, source, work, pinned=pinned)
        except Held as error:
            row["error"] = "candidate compilation refused"
            print(f"compiler candidate {ident}: compile refused: {error.reason}")
            continue
        if before != identity(measured, source, list(pinned), pinned=pinned, policy=policy):
            raise Held("try", "trial.inputs_changed: source build inputs changed during candidate compilation")
        results[ident] = result
        row["rank"] = list(measured_candidate_rank(result.compares))
        row["versions"] = {
            v: {
                "identical": c.identical,
                "of": c.of,
                "typed": c.typed,
                "match_percent": c.match_percent,
                "target_words": [f"0x{word:08X}" for word in c.target_words],
                "candidate_words": [f"0x{word:08X}" for word in c.candidate_words],
            }
            for v, c in result.compares.items()
        }
        print(
            f"compiler candidate {ident}: rank={row['rank']}; "
            + ("exact reproduction" if result.identical_everywhere else "refused as exact compiler reproduction")
        )
        for version, comparison in result.compares.items():
            if comparison.target_words or comparison.candidate_words:
                print(f"  {version} target words: " + " ".join(f"0x{w:08X}" for w in comparison.target_words))
                print(f"  {version} candidate words: " + " ".join(f"0x{w:08X}" for w in comparison.candidate_words))
            if not result.identical_everywhere:
                for line in comparison.lines:
                    print("  " + line)
    receipt = work / "compiler-candidates.json"
    receipt.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n")
    if len(results) != len(project.compiler_ties[ref]) or any(set(r.compares) != set(pinned) for r in results.values()):
        raise Held("try", f"compiler.tie_incomplete: {ref}: every candidate must compile; evidence {receipt}")
    if hashlib.sha256(source.read_bytes()).hexdigest() != evidence["source_sha256"] or any(
        hashlib.sha256(target.read_bytes()).hexdigest() != evidence["targets"][version]
        for version, (_, target) in pinned.items()
    ):
        raise Held("try", "compiler.tie_stale: source or target changed during candidate comparisons")
    exact = [ident for ident, result in results.items() if result.identical_everywhere]
    evidence["exact_candidates"] = exact
    if exact:
        for ident in results:
            if ident not in exact:
                evidence["excluded_evidence"][ident] = {
                    "source_sha256": evidence["source_sha256"],
                    "targets": evidence["targets"],
                    "comparison": evidence["candidates"][ident],
                }
    evidence["excluded_candidates"] = sorted(evidence["excluded_evidence"])
    if len(exact) > 1:
        winner, rule = compiler_ties.equivalent_choice(project, source, exact)
        evidence["reason"] = "equivalent"
        evidence["build_rule"] = rule
    else:
        best = min(measured_candidate_rank(result.compares) for result in results.values())
        winners = [ident for ident, result in results.items() if measured_candidate_rank(result.compares) == best]
        if len(winners) != 1:
            raise Held("try", f"compiler.tie_equivalent: {ref}: non-exact candidates still tied; evidence {receipt}")
        winner = winners[0]
        evidence["reason"] = "unique exact reproduction" if exact else "strictly better measured rank"
    evidence["selected"] = winner
    evidence["generations"] = {v: g.relative_to(project.root).as_posix() for v, (g, _) in pinned.items()}
    receipt.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n")
    resolved = compiler_ties.selected(project, evidence)
    if evidence["reason"] == "equivalent":
        print(f"compiler equivalent {source.stem}: {{{', '.join(exact)}}}; build {winner}; {rule}")
    else:
        print(f"compiler pin {ref}: {winner}; {evidence['reason']}; trial receipt records candidate evidence")
    return resolved, evidence
