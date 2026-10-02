"""Publish owner-bar drafts with an assembly-backed NON_MATCHING guard."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from unbake.decomp import checks, drafts, fuzzy_bar, work
from unbake.layout import name_transaction, split
from unbake.layout.name_transaction import Change
from unbake.match import declarations, queue
from unbake.project.config import Held, Policy, Project
from unbake.project.flow import WorkManifest


def admit(project: Project, policy: Policy, source: Path) -> dict[str, Any]:
    """Check latest current inputs and all containing versions without trusting a score."""
    source = source.resolve()
    rows = drafts.Store(policy, project).rows(source.stem)
    content = source.read_bytes()
    if not rows or rows[-1]["source_sha256"] != drafts.source_identity(content):
        raise Held("submit", f"trial.source_sha256: {source} changed or has no latest trial; run unbake try {source}")
    latest = rows[-1]
    versions = queue.holding_versions(project, source.stem)
    blockers = [f"{source}:{checks.message(f)}" for f in checks.run(content.decode()) if f.fakematch is None]
    verdict = fuzzy_bar.evaluate(latest["compares"], versions, [*latest["preconditions"], *blockers])
    if not verdict.passed:
        raise Held("submit", "submit.owner_fuzzy_bar: " + "; ".join(verdict.reasons))
    for version in versions:
        owners = [row for row in split.functions(project, version) if source.stem in row.aliases]
        if len(owners) != 1 or owners[0].kind != "asm":
            raise Held("submit", f"submit.nonmatching.asm.{version}: {source.stem} requires one retained asm row")
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
    ):
        if recorded.get(key) != current[key]:
            raise Held("submit", f"submit.{key}: changed since latest try")
    if recorded.get("evidence", {}).get("config_sha256") != current["evidence"]["config_sha256"]:
        raise Held("submit", "submit.config_sha256: changed since latest try")
    work.header_edits(project, current)
    return dict(current)


def publish_source(project: Project, policy: Policy, source: Path) -> list[str]:
    """Exact trials use the matched path; passing fuzzy trials keep all asm rows."""
    source = source.resolve()
    rows = drafts.Store(policy, project).rows(source.stem)
    if rows and rows[-1]["identical_everywhere"]:
        return queue.publish_source(project, policy, source)
    manifest = admit(project, policy, source)
    versions = tuple(manifest["versions"])
    text = drafts.canonical_source(source.read_bytes()).decode()
    folded = declarations.folded_edits(project, policy, source.stem, text, versions)
    headers = [edit for edit in folded if any(edit.path.is_relative_to(root) for root in project.include)]
    final = next(edit.after for edit in folded if edit.path == project.src / source.name)
    blockers = [f for f in checks.run(final) if f.fakematch is None]
    if blockers:
        raise Held("submit", "submit.source_rules: " + "; ".join(checks.message(f) for f in blockers))
    destination = project.src / source.name
    if destination.exists() and not drafts.is_partial(destination.read_text()):
        raise Held("submit", f"submit.nonmatching.source: {destination}: matched source already exists")
    changes = {
        edit.path: Change(edit.path, edit.before.encode() if edit.path.exists() else None, edit.after.encode())
        for edit in [*work.header_edits(project, cast(WorkManifest, manifest)), *headers]
    }
    guarded = ("#ifdef NON_MATCHING\n" + final.rstrip("\n") + "\n#endif\n").encode()
    changes[destination] = Change(destination, destination.read_bytes() if destination.exists() else None, guarded)

    def verify() -> None:
        admit(project, policy, source)

    results = name_transaction.apply(project, policy, list(changes.values()), verify=verify)
    return [
        f"OK(submit): {source.stem} published as NON_MATCHING; asm rows retained on {', '.join(versions)}",
        *(f"OK(submit): {result.version}: {result.sha1_line}" for result in results),
    ]
