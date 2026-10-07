"""The command output contract: one JSON result on stdout, all human text on stderr."""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field
from typing import Any, TextIO

from unbake import tui
from unbake.config import Held

EXIT = {"ok": 0, "held": 1}
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
        from unbake.process import fault

        body = {"phase": error.phase, "reason": error.reason, "fault": fault(error), **error.data, **(data or {})}
        return cls(command, "held", error.key, body, next_)

    def document(self) -> dict[str, Any]:
        return {
            "v": 1,
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
    label = {"ok": "OK", "held": "HELD"}[result.status]
    for line in result.receipts:
        print(line if _RENDERED.match(line) else f"{label}({result.command}): {line}", file=stream)
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
