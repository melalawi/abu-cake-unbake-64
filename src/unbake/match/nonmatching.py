"""Publish owner-bar drafts with an assembly-backed NON_MATCHING guard."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from unbake.decomp import checks, drafts, fuzzy_bar, work
from unbake.layout import split
from unbake.project.config import Held, Policy, Project


def admit(project: Project, policy: Policy, source: Path) -> dict[str, Any]:
    """Check latest current inputs and all containing versions without trusting a score."""
    source = source.resolve()
    rows = drafts.Store(policy, project).rows(source.stem)
    content = source.read_bytes()
    if not rows or rows[-1]["source_sha256"] != drafts.source_identity(content):
        raise Held("submit", f"trial.source_sha256: {source} changed or has no latest trial; run unbake try {source}")
    latest = rows[-1]
    versions = split.holding_versions(project, source.stem)
    blockers = [f"{source}:{checks.message(f)}" for f in checks.run(content.decode()) if f.fakematch is None]
    verdict = fuzzy_bar.evaluate(latest["compares"], versions, [*latest["preconditions"], *blockers])
    if not verdict.passed:
        raise Held("submit", "submit.owner_fuzzy_bar: " + "; ".join(verdict.reasons))
    for version in versions:
        owners = [row for row in split.functions(project, version) if source.stem in row.aliases]
        if len(owners) != 1 or owners[0].kind != "asm":
            raise Held("submit", f"submit.nonmatching.asm.{version}: {source.stem} requires one retained asm row")
    destination = project.src / source.name
    if destination.exists() and not drafts.is_partial(destination.read_text()):
        raise Held("submit", f"submit.nonmatching.source: {destination}: matched source already exists")
    current = work.current_trial(project, policy, source, list(versions), latest["work"])
    return dict(current)
