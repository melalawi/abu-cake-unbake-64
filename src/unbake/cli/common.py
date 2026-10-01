"""Argument validation and phase receipts."""

import argparse
from collections.abc import Iterable
from typing import Any, NoReturn, Protocol

from unbake.project.config import Held


class Parser(argparse.ArgumentParser):
    def __init__(self, *args: Any, phase: str = "config", **kwargs: Any) -> None:
        self.phase = phase
        kwargs["allow_abbrev"] = False
        super().__init__(*args, **kwargs)

    def error(self, message: str) -> NoReturn:
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
        if line.startswith("HELD("):
            refused = True
        if line.startswith(("OK(", "HELD(")):
            print(line)
        else:
            print(f"OK({phase}): {line}")
    if not emitted:
        print(f"OK({phase}): no entries")
    return refused


class Subparsers(Protocol):
    """The public parser registration interface used by phase commands."""

    def add_parser(self, name: str, **kwargs: Any) -> argparse.ArgumentParser: ...
