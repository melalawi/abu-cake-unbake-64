"""Public workflow commands with the active policy selection."""

import os
from pathlib import Path

from unbake.config import Project


def prefix(project: Project) -> list[str]:
    command = ["unbake", "--project", str(project.root)]
    policy = os.environ.get("UNBAKE_POLICY")
    if policy:
        command.extend(["--policy", str(Path(policy).expanduser().absolute())])
    return command
