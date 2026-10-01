"""Select one executable next action without claiming work or changing facts."""

import json
import shlex

from unbake.cli.guidance import command
from unbake.decomp import drafts, plan, work
from unbake.decomp.assign import Ledger
from unbake.decomp.trial_target import owning_versions
from unbake.layout import split
from unbake.project.config import Held, Policy, Project


def select(project: Project, policy: Policy) -> tuple[str, str]:
    occupied = {name for row in Ledger(project, policy).open() for name in (row["function"], *row["names"].values())}
    store = drafts.Store(policy, project)
    # Editable current work takes precedence over another fresh draft.
    for path in sorted(project.drafts.glob("*/manifest.json")):
        manifest = json.loads(path.read_bytes())
        if (manifest.get("schema"), manifest.get("project_id"), manifest.get("workspace_id")) != (
            1,
            project.id,
            project.workspace_id,
        ):
            continue
        subject = manifest.get("subject")
        if not isinstance(subject, str) or subject in occupied:
            continue
        source = project.root / manifest["source"]
        if not source.resolve().is_relative_to(project.drafts.resolve()) or not source.is_file():
            continue
        owners = [r for r in split.functions(project, project.names_from) if subject in r.aliases]
        if not owners or all(row.kind == "c" for row in owners):
            continue
        history = store.rows(subject)
        verb = "try"
        reason = "editable draft has not been tried with its current inputs"
        if history:
            latest = history[-1]
            try:
                current = dict(work.identity(project, source, owning_versions(project, subject, None)))
                if latest["work"] == current:
                    if latest["identical_everywhere"] and not latest["preconditions"]:
                        verb, reason = "submit", "latest exact trial covers every holding version"
                    else:
                        reason = "edit this draft to resolve the measured differences, then try again"
            except Held:
                reason = "draft inputs changed; refresh the named inputs before trying again"
        return command(project.root, verb) + " " + shlex.quote(str(source)), f"{subject}: {reason}"
    rows = plan.actionable(project, policy)
    if rows:
        row = rows[0]
        reason = f"{row.function}: supported compiler and complete function boundary on {', '.join(row.versions)}; "
        reason += (
            f"best retained score {row.score:g}%"
            if row.score is not None
            else f"smallest supported draft ({row.size} bytes)"
        )
        return command(project.root, "draft") + " " + shlex.quote(row.function), reason
    if plan.ranked(project, policy):
        return command(
            project.root, "decomp"
        ) + " plan", "next.project: remaining items need boundary, ownership or claim resolution"
    return "No unfinished items.", "all supported functions are matched"
