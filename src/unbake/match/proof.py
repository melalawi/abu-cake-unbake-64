"""Require the latest exact trial for all current publication inputs."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from unbake.decomp import drafts, work
from unbake.project.config import Held, Policy, Project


def source(project: Project, path: Path) -> Path:
    """Keep original tried bytes and their adjacent header overlay together."""
    return path


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
    current = work.identity(project, source, list(versions), policy=policy)
    recorded = latest["work"]
    for key in (
        "schema",
        "project_id",
        "workspace_id",
        "source_sha256",
        "overlay_sha256",
        "names_from",
        "versions",
        "rom_sha1",
        "compiler_sha256",
        "flags",
        "target_sha256",
        "generation_sha256",
        "layout_sha256",
        "entries",
    ):
        if recorded.get(key) != current[key]:
            raise Held("submit", f"submit.{key}: changed since latest try")
    if recorded.get("evidence", {}).get("config_sha256") != current["evidence"]["config_sha256"]:
        raise Held("submit", "submit.config_sha256: changed since latest try")
    work.header_edits(project, current)
    return dict(current)
