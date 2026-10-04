"""Read-only state guidance shared by the terminal receipt and next command."""

import hashlib
import json
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
    if root is not None and missing == "setup.roms":
        project = config.load_pending(root)
        return f"Put ROMs in {project.roms}. Then run {retry}."
    if missing == "headers.declaration":
        return f"Repair the SDK/shared header prerequisite identified above. Then run {retry}."
    if missing is not None and missing.startswith("split."):
        if missing == "split.data_to_code":
            return command(root, "split") + " code FUNCTION --version V --start ROM --end ROM"
        if missing.startswith("split.code."):
            return command(root, "split") + " code --help"
        if missing.startswith("split.cut."):
            return "Correct the selected interval and owner with " + command(root, "split") + " cut --help."
    if root is not None and missing is not None and missing.startswith(("types.", "map.")):
        phase = "map" if missing.startswith("map.") and missing != "map.inputs_stale" else "solve"
        tokens = shlex.split(retry)
        return shlex.join([*tokens[:-1], phase])
    if missing == "setup.compiler_confirmation" and root is not None:
        path = config.load_pending(root).build / "setup/proposal.json"
        if path.is_file():
            proposal = path.read_bytes()
            if not json.loads(proposal).get("unresolved"):
                refresh = " --repropose-compilers" if config.load_pending(root).state == "ready" else ""
                return retry + refresh + " --confirm " + hashlib.sha256(proposal).hexdigest()
    if root is not None and missing in {
        "trial.source_sha256",
        "submit.source_sha256",
        "submit.overlay_sha256",
        "trial.receipt",
        "submit.compiler_candidates",
        "submit.target_sha256",
        "submit.flags",
        "submit.compiler_sha256",
        "submit.layout_sha256",
        "submit.exact",
        "submit.versions",
        "draft.source",
    }:
        try:
            from unbake.cli.workflow import select

            return select(config.load(root), config.load_policy())[0]
        except (Held, OSError, ValueError):
            pass
    if missing is not None:
        return f"Repair the prerequisite identified above. Then run {retry}."
    if root is None:
        return "unbake init <name>"
    try:
        project = config.load_pending(root)
    except Held as error:
        return error.next_action or f"Repair the project configuration identified above. Then run {retry}."
    if project.state == "awaiting-roms":
        setup = command(root, "setup")
        if project.roms.is_dir() and next(project.roms.iterdir(), None) is not None:
            return setup
        return f"Put ROMs in {project.roms}. Then run {setup}."
    try:
        from unbake.cli.workflow import select

        return select(config.load(root), config.load_policy())[0]
    except (Held, OSError, ValueError) as error:
        if isinstance(error, Held) and error.next_action is not None:
            return error.next_action
        return f"Repair the prerequisite identified above. Then run {command(root, 'next')}."
