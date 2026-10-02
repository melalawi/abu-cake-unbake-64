"""Argument validation and phase receipts."""

import argparse
import sys
from collections.abc import Iterable
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, NoReturn, Protocol

from unbake.project.config import Held


@dataclass
class Invocation:
    refused: bool = False
    next_action: str | None = None
    missing_input: str | None = None


_invocation: ContextVar[Invocation | None] = ContextVar("invocation", default=None)


def begin() -> Invocation:
    invocation = Invocation()
    _invocation.set(invocation)
    return invocation


def suggest(action: str) -> None:
    """Commands hand their next action to the single terminal receipt owner."""
    invocation = _invocation.get()
    if invocation is not None:
        invocation.next_action = action.removeprefix("Next: ")


def finish(action: str, *, json_output: bool = False) -> None:
    print(f"Next: {action}", file=sys.stderr if json_output else sys.stdout)
    _invocation.set(None)


class Parser(argparse.ArgumentParser):
    def __init__(self, *args: Any, phase: str = "config", **kwargs: Any) -> None:
        self.phase = phase
        kwargs["allow_abbrev"] = False
        super().__init__(*args, **kwargs)

    def error(self, message: str) -> NoReturn:
        if self.phase == "init" and "NAME" in message:
            message = "init.target: missing NAME"
        elif message.startswith("the following arguments are required: "):
            message = message.removeprefix("the following arguments are required: ") + ": missing required input"
        raise Held(self.phase, message)


def integer(value: str) -> int:
    try:
        return int(value, 0)
    except ValueError as error:
        raise argparse.ArgumentTypeError(f"{value!r}: expected integer or 0x address") from error


def count(value: str) -> int:
    number = integer(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("--count: expected positive integer")
    return number


def receipt(phase: str, lines: Iterable[object]) -> bool:
    """Print phase receipts once and preserve per-function refusal status."""
    refused = False
    emitted = False
    for entry in lines:
        emitted = True
        line = str(entry)
        if line.startswith(("HELD(", "HELD:")):
            refused = True
            invocation = _invocation.get()
            if invocation is not None and invocation.missing_input is None:
                invocation.missing_input = (
                    "headers.declaration"
                    if "headers.declaration:" in line
                    else line.split(":", 1)[1].strip().split(":", 1)[0]
                )
        if line.startswith("HELD:"):
            line = f"HELD({phase}): {line.removeprefix('HELD:').strip()}"
        if line.startswith(("OK(", "HELD(")):
            print(line, flush=True)
        else:
            print(f"OK({phase}): {line}")
    if not emitted:
        print(f"OK({phase}): no entries")
    invocation = _invocation.get()
    if invocation is not None:
        invocation.refused |= refused
    return refused


class Subparsers(Protocol):
    """The public parser registration interface used by phase commands."""

    def add_parser(self, name: str, **kwargs: Any) -> argparse.ArgumentParser: ...
