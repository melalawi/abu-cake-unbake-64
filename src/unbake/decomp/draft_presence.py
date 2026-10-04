"""Workspace-local draft attempts and private draft discovery for selection."""

import json
import os
from pathlib import Path

from unbake.decomp.trial_compile import default_scratch
from unbake.project.config import Policy, Project, relative_text
from unbake.project_tools import atomic


def remember(project: Project, function: str, scratch: Path, reason: str) -> None:
    atomic.text(
        project.drafts / function / "attempt.json",
        json.dumps(
            {
                "project_id": project.id,
                "subject": function,
                "scratch": os.path.relpath(scratch, project.root),
                "reason": relative_text(project.root, reason),
            },
            sort_keys=True,
        )
        + "\n",
    )


def attempts(project: Project) -> dict[str, dict[str, str]]:
    output = {}
    for path in sorted(project.drafts.glob("*/attempt.json")):
        value = json.loads(path.read_bytes())
        if value.get("project_id") == project.id:
            output[value["subject"]] = value
    return output


def private(project: Project, policy: Policy) -> dict[str, str]:
    output = {name: row["reason"] for name, row in attempts(project).items()}
    # Discover default-scratch drafts created before attempt receipts existed.
    for path in sorted((default_scratch(project, policy) / "drafts").glob("*/manifest.json")):
        value = json.loads(path.read_bytes())
        subject = value.get("subject")
        if (
            (value.get("schema"), value.get("project_id")) == (1, project.id)
            and isinstance(subject, str)
            and (path.parent / (subject + ".c")).is_file()
        ):
            output[subject] = "private draft"
    return output
