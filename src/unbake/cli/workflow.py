"""Select one executable next action without claiming work or changing facts."""

import json
import shlex
from pathlib import Path

from unbake.cli.guidance import command
from unbake.decomp import draft_presence, drafts, exclusions, fuzzy_bar, plan, type_context, work
from unbake.decomp.assign import Ledger
from unbake.decomp.trial_target import owning_versions
from unbake.layout import split
from unbake.config import Held, Host, Project, load_policy


def select(project: Project, policy: Host, *, exclude: Path | None = None, new: bool = False) -> tuple[str, str]:
    excluded = exclusions.load(project, exclude)

    def draft_command(subject: str) -> str:
        action = command(project.root, "draft") + " " + shlex.quote(subject)
        if exclude is not None:
            action += " --exclude " + shlex.quote(str(exclude.resolve()))
        return action

    local = project.tools / "clone-policy.toml"
    if local.is_file():
        policy = load_policy(local)
    if not (project.build / "map/facts.json").is_file():
        return command(
            project.root, "map"
        ), "whole-program register, call and memory facts are required before drafting"
    if not (project.build / "types/database.json").is_file():
        return command(project.root, "solve"), "solve shared type constraints before drafting"
    redrafts = {} if new else type_context.redrafts(project)
    occupied = {name for row in Ledger(project, policy).open() for name in (row["function"], *row["names"].values())}
    store = drafts.Store(policy, project)
    actions = []
    seen = draft_presence.private(project, policy) if new else {}
    inventory: list[split.Function] | None = None
    # Editable current work takes precedence over another fresh draft.
    for path in sorted(project.drafts.glob("*/manifest.json")):
        manifest = json.loads(path.read_bytes())
        if (manifest.get("schema"), manifest.get("project_id")) != (
            1,
            project.id,
        ):
            continue
        subject = manifest.get("subject")
        if not isinstance(subject, str) or subject in occupied or subject in excluded:
            continue
        source = project.root / manifest["source"]
        if not source.resolve().is_relative_to(project.drafts.resolve()) or not source.is_file():
            continue
        seen[subject] = "editable draft"
        if new:
            continue
        if inventory is None:
            inventory = [row for version in project.versions for row in split.functions(project, version)]
        owners = [row for row in inventory if subject in row.aliases]
        if not owners or all(row.kind == "c" for row in owners):
            continue
        if subject in redrafts:
            return draft_command(subject), f"{subject}: type solution changed; redraft required"
        history = store.rows(subject)
        verb = "try"
        reason = "editable draft has not been tried with its current inputs"
        if history:
            latest = history[-1]
            try:
                work.current_trial(project, policy, source, owning_versions(project, subject, None), latest["work"])
                if fuzzy_bar.evaluate(
                    latest["compares"], owning_versions(project, subject, None), latest["preconditions"]
                ).passed:
                    verb, reason = "submit", "latest owner-bar trial covers every holding version"
                else:
                    reason = "edit this draft to resolve the measured differences, then try again"
            except Held:
                reason = "draft inputs changed; refresh the named inputs before trying again"
        score = min(history[-1]["score"].values()) if history else 0.0
        actions.append(
            (
                (1 if verb == "submit" else 2, -score, subject),
                command(project.root, verb) + " " + shlex.quote(str(source)),
                f"{subject}: {reason}",
            )
        )
    if actions:
        _, action, reason = min(actions)
        return action, reason
    skipped: list[str] = []

    def available(row: plan.Row) -> bool:
        why = "explicit exclusion" if excluded.intersection(row.aliases) else None
        if new and why is None:
            why = next((seen[name] for name in row.aliases if name in seen), None)
            if why is None and row.draft is not None:
                why = "retained draft"
        if why is not None:
            entry = f"{row.function} ({why})"
            if entry not in skipped:
                skipped.append(entry)
            return False
        return True

    def explanation(reason: str) -> str:
        if new and skipped:
            detail = ", ".join(skipped[:5])
            if len(skipped) > 5:
                detail += f"; +{len(skipped) - 5} more"
            reason += "\nSkipped: " + detail
        return reason

    rows = [row for row in plan.actionable(project, policy) if available(row)]
    if rows:
        row = rows[0]
        reason = f"{row.function}: supported compiler and complete function boundary on {', '.join(row.versions)}; "
        reason += (
            f"best retained score {row.score:g}%"
            if row.score is not None
            else f"smallest supported draft ({row.size} bytes)"
        )
        return draft_command(row.function), explanation(reason)
    if any(available(row) for row in plan.ranked(project, policy)):
        raise Held("next", "next.project: remaining items need boundary, ownership or claim resolution")
    if new:
        return "No undrafted items.", explanation("--new found no remaining supported undrafted functions")
    return "No unfinished items.", "all supported functions are matched or explicitly excluded"
