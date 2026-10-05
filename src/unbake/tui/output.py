"""Where human text goes: start() picks the renderer once, line() and verdict() write through it.

Before start() (a library call, a test) lines go to the process's stderr, undecorated."""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING, Literal, TextIO

from unbake.config import Held
from unbake.tui import progress
from unbake.tui.render import Live, Plain

if TYPE_CHECKING:
    from rich.console import Console

_stream: TextIO | None = None
_console: Console | None = None


def interactive() -> bool:
    return sys.stdin.isatty() and sys.stderr.isatty()


def stderr() -> TextIO:
    return sys.stderr


def start(stream: TextIO, interactive: bool) -> None:
    """Choose Live (a terminal) or Plain (a pipe or file) for the rest of the process."""
    global _stream, _console
    if progress._renderer is not None:
        raise Held("tui", "tui.start: called twice")
    _stream = stream
    if interactive:
        _console = console()
        progress.bind(Live(_console))
    else:
        progress.bind(Plain(stream))


def stop() -> None:
    """End the process's output choice, so a later start() (the next command of a test run) may choose again."""
    global _stream, _console
    progress.bind(None)
    _stream = _console = None


def console() -> Console:
    """The one Rich console on the output stream: the progress rows and the cycle board share it."""
    global _console
    if _console is None:
        from rich.console import Console

        _console = Console(file=_stream if _stream is not None else sys.stderr)
    return _console


def write(text: str) -> None:
    """Text that is not a line of progress (usage and help), as it is."""
    (_stream if _stream is not None else sys.stderr).write(text)


def line(text: str) -> None:
    renderer = progress._renderer
    if renderer is None:
        sys.stderr.write(text + "\n")
        return
    renderer.line(text, progress.depth())


def verdict(kind: Literal["cracked", "creative"], text: str) -> None:
    renderer = progress._renderer
    if renderer is None:
        sys.stderr.write(f"{'CRACKED' if kind == 'cracked' else 'NEEDS CREATIVE'}  {text}\n")
        return
    renderer.verdict(kind, text, progress.depth())
