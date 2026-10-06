"""Turn a refusal into a runnable Next line: a command with its arguments, or `stop: reason`."""

from __future__ import annotations

from unbake.cli.args import Context
from unbake.config import Held


def after(context: Context, error: Held) -> str:
    """The next action after a refusal that did not name its own."""
    key = error.key
    if "C identifier" in error.reason:
        return "stop: choose a valid C function identifier from unbake next"
    from unbake.process import fault

    pending = [fault(error)]
    while pending:
        value = pending.pop()
        if isinstance(value, dict):
            if value.get("category") in ("native-os", "native-signal", "native-exit"):
                return "stop: correct the native tool failure captured with this result before retrying"
            pending.extend(value.values())
        elif isinstance(value, (list, tuple)):
            pending.extend(value)
    if key in ("worker.memory", "worker.crash"):
        return "stop: review the failing unit and measured worker fault captured with this result"
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
    if key.startswith("compile."):
        # The unit's own C or a header it includes does not compile: an edit fixes it, `next` would not.
        return "stop: fix the C source named above (or the header the compiler names) so it compiles"
    if key.startswith("layout."):
        # The map step renders layout.toml from the current split before any command reads it; what still
        # refuses is an authored or proven group, which only an edit changes. `next` would reach it again.
        return "stop: fix the layout.toml group named above"
    if context.command == "next":
        return f"stop: {error.reason}"
    return context.cmd("next")
