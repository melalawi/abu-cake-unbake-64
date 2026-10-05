"""The cycle's background refresh: types, headers and build files brought current while drafts run.

One thread runs the steps; a request while it runs makes it run once more, so lands coalesce. It reports
only through the coordinator's queue: ("step", StepResult) per step that ran, then ("refreshed", Refreshed)
when no request is pending. Heavy step work goes through the pool the cycle shares (pool.sharing).
"""

from __future__ import annotations

import queue
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from unbake.config import Held, Host

STEPS = ("types", "headers", "buildfiles")


@dataclass(frozen=True)
class Refreshed:
    steps: tuple[str, ...]
    seconds: float
    diagnostic: str = ""


class Refresh:
    def __init__(self, root: Path, host: Host, inbox: queue.Queue[tuple[str, Any]], steps: Sequence[str] = STEPS):
        self.root, self.host, self.inbox, self.steps = root, host, inbox, tuple(steps)
        self._state = threading.Condition()
        self._pending = False
        self._running = False

    @property
    def busy(self) -> bool:
        with self._state:
            return self._running

    def request(self) -> None:
        """Bring the steps current; while a run is in progress, run once more after it."""
        with self._state:
            self._pending = True
            if self._running:
                return
            self._running = True
        threading.Thread(target=self._run, name="unbake-refresh", daemon=True).start()

    def join(self) -> None:
        """Wait until no run is in progress or pending."""
        with self._state:
            self._state.wait_for(lambda: not self._running)

    def _run(self) -> None:
        from unbake import config, steps

        ran: list[str] = []
        started = time.monotonic()
        diagnostic = ""
        while True:
            with self._state:
                if not self._pending or diagnostic:
                    self._running = False
                    self._pending = False
                    self._state.notify_all()
                    break
                self._pending = False
            try:
                for result in steps.ensure(
                    config.load(self.root), self.host, self.steps, report=lambda done: self.inbox.put(("step", done))
                ):
                    if result.ran:
                        ran.append(result.step)
            except Held as error:
                diagnostic = error.reason
            except Exception as error:  # the coordinator reports it; a dead thread would hang every land
                diagnostic = f"{type(error).__name__}: {error}"
        self.inbox.put(("refreshed", Refreshed(tuple(dict.fromkeys(ran)), time.monotonic() - started, diagnostic)))
