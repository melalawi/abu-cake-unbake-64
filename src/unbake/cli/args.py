"""Argument parsing into a command context, and runnable Next command spelling."""

from __future__ import annotations

import argparse
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NoReturn, Protocol, TextIO

from unbake import config, tui
from unbake.config import Held, Host, PendingProject, Project


class HelpRequested(Exception):
    def __init__(self, usage: str) -> None:
        super().__init__(usage)
        self.usage = usage


class Parser(argparse.ArgumentParser):
    """argparse that writes help to stderr and refuses usage errors as HELD(usage)."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        kwargs["allow_abbrev"] = False
        kwargs.setdefault("formatter_class", argparse.RawDescriptionHelpFormatter)
        super().__init__(*args, **kwargs)

    def error(self, message: str) -> NoReturn:
        raise Held("usage", f"usage: {self.prog}: {message}")

    def print_help(self, file: Any = None) -> None:
        tui.write(self.format_help())

    def exit(self, status: int = 0, message: str | None = None) -> NoReturn:
        if message:
            tui.write(message)
        raise HelpRequested(self.format_help())


class Subparsers(Protocol):
    def add_parser(self, name: str, **kwargs: Any) -> argparse.ArgumentParser: ...


def integer(value: str) -> int:
    try:
        return int(value, 0)
    except ValueError as error:
        raise argparse.ArgumentTypeError(f"{value!r}: expected integer or 0x address") from error


def positive(value: str) -> int:
    number = integer(value)
    if number <= 0:
        raise argparse.ArgumentTypeError(f"{value!r}: expected positive integer")
    return number


@dataclass
class Context:
    """One command invocation: the parsed arguments, project root and host config."""

    command: str
    args: argparse.Namespace
    root: Path | None
    config_path: Path | None
    stdout: TextIO
    host: Host | None = None
    _project: Project | None = field(default=None, repr=False)

    def project(self) -> Project:
        if self.root is None:
            raise Held("config", "project.root: no project; supply --project DIR")
        if self._project is None:
            self._project = config.load(self.root)
        return self._project

    def pending(self) -> PendingProject:
        if self.root is None:
            raise Held("config", "project.root: no project; supply --project DIR")
        return config.load_pending(self.root)

    def require_host(self) -> Host:
        if self.host is None:
            raise Held("config", f"unbake.toml: {self.command} needs host configuration")
        return self.host

    def ready(self, *names: str) -> tuple[Project, Host]:
        """The project and host after bringing the named derived steps up to date (steps.STEPS)."""
        from unbake import steps

        project, host = self.project(), self.require_host()
        steps.ensure(project, host, names)
        return project, host

    def cmd(self, *words: str | Path) -> str:
        """A runnable `unbake ...` line for this project, from any working directory."""
        tokens = ["unbake"]
        if self.root is not None and not Path.cwd().resolve().is_relative_to(self.root):
            tokens += ["--project", str(self.root)]
        if self.config_path is not None and self.config_path != config.host_path(None):
            tokens += ["--config", str(self.config_path)]
        return shlex.join([*tokens, *(str(word) for word in words)])
