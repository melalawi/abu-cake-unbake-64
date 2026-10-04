"""Workspace-local draft attempts and private draft discovery for selection."""

import json
from pathlib import Path

from unbake.decomp.trial_compile import default_scratch
from unbake.project.config import Policy, Project
from unbake.project_tools import atomic


def remember(project: Project, function: str, scratch: Path, reason: str) -> None:
    atomic.text(
        project.drafts / function / "attempt.json",
        json.dumps(
            {
                "project_id": project.id,
                "workspace_id": project.workspace_id,
                "subject": function,
                "scratch": str(scratch),
                "reason": reason,
            },
            sort_keys=True,
        )
        + "\n",
    )


def attempts(project: Project) -> dict[str, dict[str, str]]:
    output = {}
    for path in sorted(project.drafts.glob("*/attempt.json")):
        value = json.loads(path.read_bytes())
        if (value.get("project_id"), value.get("workspace_id")) == (project.id, project.workspace_id):
            output[value["subject"]] = value
    return output


def private(project: Project, policy: Policy) -> dict[str, str]:
    output = {name: row["reason"] for name, row in attempts(project).items()}
    # Discover default-scratch drafts created before attempt receipts existed.
    for path in sorted((default_scratch(project, policy) / "drafts").glob("*/manifest.json")):
        value = json.loads(path.read_bytes())
        subject = value.get("subject")
        if (
            (value.get("schema"), value.get("project_id"), value.get("workspace_id"))
            == (1, project.id, project.workspace_id)
            and isinstance(subject, str)
            and (path.parent / (subject + ".c")).is_file()
        ):
            output[subject] = "private draft"
    return output
