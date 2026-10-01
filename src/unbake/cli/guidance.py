"""Read-only state guidance shared by the terminal receipt and next command."""

import shlex
from pathlib import Path

from unbake.project import config
from unbake.project.config import Held


def command(root: Path | None, phase: str) -> str:
    tokens = ["unbake"]
    if root is not None and not Path.cwd().resolve().is_relative_to(root):
        tokens += ["--project", str(root)]
    return shlex.join([*tokens, phase])


def resolve(root: Path | None, *, missing: str | None = None, retry: str = "unbake setup") -> str:
    if missing is not None:
        return f"Supply {missing}. Then run {retry}."
    if root is None:
        return "unbake init <name>"
    try:
        project = config.load_pending(root)
    except Held as error:
        return f"Supply {error.reason.split(':', 1)[0]}. Then run {retry}."
    if project.state == "awaiting-roms":
        setup = command(root, "setup")
        if project.roms.is_dir() and next(project.roms.iterdir(), None) is not None:
            return setup
        return f"Put ROMs in {project.roms}. Then run {setup}."
    return command(root, "next")
