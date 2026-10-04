"""Turn a refusal into a runnable Next line: a command with its arguments, or `stop: reason`."""

from __future__ import annotations

from unbake.cli.args import Context
from unbake.config import Held


def after(context: Context, error: Held) -> str:
    """The next action after a refusal that did not name its own."""
    key = error.key
    if error.phase == "usage":
        return context.cmd(context.command, "--help")
    if key.startswith("unbake.toml"):
        return "stop: fix the host config named above (see `unbake --help` and the README)"
    if key == "project.root":
        return "stop: run inside a project directory or pass --project DIR"
    if key == "project.state":
        return context.cmd("setup")
    if key == "project.lock":
        return "stop: wait for the other writer named above to finish"
    if key.endswith("config.toml") or ".toml [" in error.reason.split(":", 1)[0]:
        return "stop: fix the project config.toml value named above"
    if context.command == "next":
        return f"stop: {error.reason}"
    return context.cmd("next")
