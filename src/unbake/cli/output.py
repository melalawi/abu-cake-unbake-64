"""The command output contract: one JSON result on stdout, all human text on stderr."""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field, replace
from typing import Any, TextIO

from unbake import tui
from unbake.config import Held

EXIT = {"ok": 0, "held": 1, "interrupted": 130}
# A receipt already rendered for people (OK(setup): ..., HELD(check): ...); JSON keeps only the text after it.
_RENDERED = re.compile(r"^[A-Z]+\([\w-]+\): ")


@dataclass(frozen=True)
class Result:
    command: str
    status: str
    key: str | None = None
    data: dict[str, Any] = field(default_factory=dict)
    next: str | None = None
    receipts: tuple[str, ...] = ()
    # What the command cost (effort.Effort.document), set by main before the result is printed.
    effort: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def ok(cls, command: str, data: dict[str, Any], receipts: list[str] | tuple[str, ...], next_: str | None) -> Result:
        return cls(command, "ok", None, data, next_, tuple(receipts))

    @classmethod
    def held(cls, command: str, error: Held, next_: str | None, data: dict[str, Any] | None = None) -> Result:

        body = {
            "phase": error.phase,
            "reason": error.reason,
            "cause_id": error.fault.cause.id,
            "retryability": error.fault.cause.retryability,
            "fault": error.fault.document(),
            **error.data,
            **(data or {}),
        }
        return cls(command, "held", error.key, body, next_)

    @classmethod
    def interrupted(cls, command: str, next_: str | None, data: dict[str, Any] | None = None) -> Result:
        """An unfinished command has no refusal or final cause; its work remains retryable."""
        return cls(command, "interrupted", None, {**(data or {}), "retryable": True}, next_)

    def render_actions(self, context: Any) -> Result:
        from unbake.process import Action

        def render(value: Any) -> Any:
            if isinstance(value, Action):
                return value.render(context)
            if isinstance(value, dict):
                return {key: render(item) for key, item in value.items()}
            if isinstance(value, (list, tuple)):
                return [render(item) for item in value]
            return value

        return replace(self, data=render(self.data), next=render(self.next))

    def document(self) -> dict[str, Any]:
        return {
            "v": 2,
            "command": self.command,
            "status": self.status,
            "key": self.key,
            "data": self.data,
            "next": self.next,
            "receipts": [_RENDERED.sub("", line) for line in self.receipts],
            "effort": self.effort,
        }


def human(result: Result, stream: TextIO) -> None:
    """Receipts as OK(...) lines, the HELD line, then the Next line."""
    label = {"ok": "OK", "held": "HELD", "interrupted": "INTERRUPTED"}[result.status]
    for line in result.receipts:
        print(line if _RENDERED.match(line) else f"{label}({result.command}): {line}", file=stream)
    if result.status == "interrupted":
        print(f"INTERRUPTED({result.command}): work remains ready to retry", file=stream)
    if result.status == "held":
        phase, reason = result.data.get("phase", result.command), result.data.get("reason", result.key)
        print(f"HELD({phase}): {reason}", file=stream)
    if result.next is not None:
        print(f"Next: {result.next}", file=stream)


def emit(result: Result, stdout: TextIO | None = None, stderr: TextIO | None = None) -> int:
    """Write the JSON document to stdout and the human text to stderr; return the exit code."""
    out = sys.stdout if stdout is None else stdout
    err = tui.stderr() if stderr is None else stderr
    human(result, err)
    out.write(json.dumps(result.document(), sort_keys=True) + "\n")
    out.flush()
    return EXIT[result.status]


def line(stream: TextIO, event: dict[str, Any]) -> None:
    """One JSON line of a stream (cycle events, --list rows)."""
    stream.write(json.dumps(event, sort_keys=True) + "\n")
    stream.flush()
