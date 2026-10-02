"""Require the latest exact trial for all current publication inputs."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from unbake.decomp import drafts, work
from unbake.project.config import Held, Policy, Project


def ensure(project: Project, policy: Policy, source: Path, versions: tuple[str, ...]) -> dict[str, Any]:
    rows = drafts.Store(policy, project).rows(source.stem)
    digest = drafts.source_identity(source.read_bytes())
    if not rows or rows[-1]["source_sha256"] != digest:
        raise Held("submit", f"trial.source_sha256: {source} changed or has no latest trial; run unbake try {source}")
    latest = rows[-1]
    if set(latest["compares"]) != set(versions):
        raise Held(
            "submit",
            f"submit.versions: {source.stem} latest trial compares must cover every owner: {', '.join(versions)}",
        )
    if not latest["identical_everywhere"] or latest["preconditions"]:
        raise Held("submit", f"submit.exact: {source.stem} requires identical_everywhere=true and resolved needs")
    current = work.current_trial(project, policy, source, list(versions), latest["work"])
    return dict(current)
