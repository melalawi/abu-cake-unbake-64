"""The two renderers: Plain (timestamped lines on a stream) and Live (Rich rows on the board's console)."""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, TextIO

if TYPE_CHECKING:
    from rich.console import Console
    from rich.progress import Progress, TaskID

    from unbake.tui.progress import Task

PROGRESS_EVERY = 10.0
BOLD_GREEN = "\x1b[1;32m"
BOLD_YELLOW = "\x1b[1;33m"
RESET = "\x1b[0m"
HEADING = {"cracked": "CRACKED", "creative": "NEEDS CREATIVE"}
STYLE = {"cracked": "bold green", "creative": "bold yellow"}
ANSI = {"cracked": BOLD_GREEN, "creative": BOLD_YELLOW}


def duration(seconds: float) -> str:
    whole = int(seconds)
    if whole < 60:
        return f"{whole}s"
    if whole < 3600:
        return f"{whole // 60}m {whole % 60}s"
    return f"{whole // 3600}h {whole % 3600 // 60}m"


def percent(task: Task) -> str:
    return "" if not task.total else f" ({100 * task.done // task.total}%)"


class Plain:
    """Lines on a stream: HH:MM:SS, two spaces of indent per depth, the words."""

    def __init__(
        self,
        stream: TextIO,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
        color: bool | None = None,
    ) -> None:
        self.stream, self.clock, self.wall = stream, clock, wall
        self.color = bool(os.environ.get("FORCE_COLOR")) if color is None else color
        self.shown: dict[int, float] = {}

    def _write(self, text: str, depth: int) -> None:
        stamp = time.strftime("%H:%M:%S", time.localtime(self.wall()))
        self.stream.write(f"{stamp}  {'  ' * depth}{text}\n")
        self.stream.flush()

    def start(self, task: Task) -> None:
        self.shown[id(task)] = self.clock()
        self._write(task.label, task.depth)

    def progress(self, task: Task) -> None:
        now = self.clock()
        if task.total is None or now - self.shown.get(id(task), now) < PROGRESS_EVERY:
            return
        self.shown[id(task)] = now
        self._write(f"{task.label}: {task.done} of {task.total}{percent(task)}", task.depth)

    def done(self, task: Task, seconds: float, cores: float, extra: str) -> None:
        self.shown.pop(id(task), None)
        self._write(f"{task.label}: done in {duration(seconds)}, {cores:.1f} cores{extra}", task.depth)

    def line(self, text: str, depth: int) -> None:
        self._write(text, depth)

    def verdict(self, kind: str, text: str, depth: int) -> None:
        heading = f"{HEADING[kind]}  {text}"
        self._write(f"{ANSI[kind]}{heading}{RESET}" if self.color else heading, depth)


class Live:
    """A Rich Progress with one row per open task, shown while any task is open. It adds no colour of its own."""

    def __init__(self, console: Console) -> None:
        from rich.progress import BarColumn, Progress, TaskProgressColumn, TextColumn, TimeElapsedColumn

        self.console = console
        self.bars: Progress = Progress(
            TextColumn("{task.description}"),
            BarColumn(style="", complete_style="", finished_style="", pulse_style=""),
            TaskProgressColumn(),
            TimeElapsedColumn(),
            console=console,
            transient=True,
        )
        self.rows: dict[int, TaskID] = {}

    def _print(self, text: str, depth: int, style: str | None = None) -> None:
        self.console.print(f"{'  ' * depth}{text}", style=style, markup=False, highlight=False, soft_wrap=True)

    def start(self, task: Task) -> None:
        if not self.rows:
            self.bars.start()
        self.rows[id(task)] = self.bars.add_task(f"{'  ' * task.depth}{task.label}", total=task.total)

    def progress(self, task: Task) -> None:
        row = self.rows.get(id(task))
        if row is not None:
            self.bars.update(row, completed=task.done, total=task.total)

    def done(self, task: Task, seconds: float, cores: float, extra: str) -> None:
        row = self.rows.pop(id(task), None)
        if row is not None:
            self.bars.remove_task(row)
        if not self.rows:
            self.bars.stop()
        self._print(f"{task.label}: done in {duration(seconds)}, {cores:.1f} cores{extra}", task.depth)

    def line(self, text: str, depth: int) -> None:
        self._print(text, depth)

    def verdict(self, kind: str, text: str, depth: int) -> None:
        self._print(f"{HEADING[kind]}  {text}", depth, STYLE[kind])
