"""The human side of `unbake cycle`: the pick screen (questionary) and the board (Rich), both on stderr.

The board redraws on events only. A key-reader thread turns keys into coordinator messages:
r compare again, d redraft, h hold or release, e open the file in $VISUAL/$EDITOR, q quit, arrows select.
"""

from __future__ import annotations

import os
import queue
import subprocess
import sys
import termios
import threading
import time
import tty
from typing import TYPE_CHECKING, Any

from unbake.config import Held, Project
from unbake.cycle import rank

if TYPE_CHECKING:
    from unbake.cycle.engine import Row

HELP = "↑↓ select · enter details · e edit · r compare again · d redraft · h hold/release · q quit · ? help"


def _label(row: rank.Candidate) -> str:
    mark = "↻ " if row.carryover else "  "
    best = f"best {row.best_percent:.1f}%" if row.best_percent is not None else "new"
    return f"{mark}{row.function}  {row.bytes} B  {best}  [{' '.join(row.versions)}]"


def pick(order: list[rank.Candidate]) -> list[rank.Candidate]:
    import questionary

    if not order:
        return []
    auto = min(5, len(order))
    choices = [questionary.Choice(f"auto {auto}", value="auto", checked=True)]
    choices += [questionary.Choice(_label(row), value=row.function) for row in order[:200]]
    answer = questionary.checkbox("Pick functions (bytes per minute)", choices=choices, qmark="?").unsafe_ask()
    if not answer:
        raise Held("cycle", "cycle.pick: nothing picked")
    if "auto" in answer:
        return order[:auto]
    chosen = set(answer)
    return [row for row in order if row.function in chosen]


class Board:
    """Rich Live board on stderr; keys go to the coordinator's inbox as ("key", (action, function))."""

    def __init__(self, project: Project, rows: dict[str, Row], inbox: queue.Queue[tuple[str, Any]]) -> None:
        from rich.console import Console
        from rich.live import Live

        self.project, self.rows, self.inbox = project, rows, inbox
        self.selected = 0
        self.detail = ""
        self.console = Console(file=sys.stderr)
        self.live = Live(self.render(), console=self.console, auto_refresh=False, transient=False)
        self.live.start()
        self.stopped = threading.Event()
        self.reader = threading.Thread(target=self._keys, name="keys", daemon=True)
        self.reader.start()

    def names(self) -> list[str]:
        return list(self.rows)

    def render(self) -> Any:
        from rich.table import Table

        table = Table(title=f"{self.project.name}  {' '.join(self.project.versions)}", expand=True, show_lines=False)
        for column in ("function", "bytes", "stage", "best", "tries", "elapsed", "last"):
            table.add_column(column)
        for index, row in enumerate(self.rows.values()):
            elapsed = time.monotonic() - row.started
            style = "reverse" if index == self.selected else ""
            table.add_row(
                row.function,
                str(row.bytes),
                row.stage,
                "" if row.best_percent is None else f"{row.best_percent:.1f}%",
                str(row.tries),
                f"{int(elapsed // 60):02d}:{int(elapsed % 60):02d}",
                (row.commit[:7] if row.commit else row.diagnostic)[:60],
                style=style,
            )
        landed = sum(row.bytes for row in self.rows.values() if row.stage == "landed")
        held = sum(1 for row in self.rows.values() if row.stage in ("held", "failed"))
        table.caption = f"landed {landed} B · held {held} · {HELP}" + (f"\n{self.detail}" if self.detail else "")
        return table

    def on_event(self, _record: dict[str, Any]) -> None:
        self.live.update(self.render(), refresh=True)

    def _keys(self) -> None:
        stream = sys.stdin
        descriptor = stream.fileno()
        saved = termios.tcgetattr(descriptor)
        try:
            tty.setcbreak(descriptor)
            while not self.stopped.is_set():
                key = stream.read(1)
                if key == "\x1b":
                    sequence = stream.read(2)
                    key = {"[A": "up", "[B": "down"}.get(sequence, "")
                self._handle(key)
        finally:
            termios.tcsetattr(descriptor, termios.TCSADRAIN, saved)

    def _handle(self, key: str) -> None:
        names = self.names()
        if not names:
            return
        function = names[min(self.selected, len(names) - 1)]
        if key == "up":
            self.selected = max(0, self.selected - 1)
        elif key == "down":
            self.selected = min(len(names) - 1, self.selected + 1)
        elif key in ("\n", "\r"):
            row = self.rows[function]
            self.detail = f"{function}: {row.diagnostic or 'no diagnostic'}"
        elif key == "?":
            self.detail = HELP
        elif key in ("r", "d", "h"):
            self.inbox.put(("key", (key, function)))
        elif key == "e":
            self._edit(function)
        elif key == "q":
            self.inbox.put(("quit", None))
        self.live.update(self.render(), refresh=True)

    def _edit(self, function: str) -> None:
        editor = os.environ.get("VISUAL") or os.environ.get("EDITOR")
        file = self.rows[function].file
        if not editor:
            self.detail = "edit: set VISUAL or EDITOR to open files from the board"
            return
        if not file:
            self.detail = f"edit: {function} has no draft yet"
            return
        self.live.stop()
        try:
            subprocess.run([*editor.split(), file], check=False)
        finally:
            self.live.start()

    def close(self) -> None:
        self.stopped.set()
        self.live.stop()
