"""The mechanical search ladder a draft climbs before it needs an edit by hand.

A function whose compare is not exact is searched with each built-in method in turn (search.BUILTINS order),
each starting from the best text so far. A method that brings no gain over the best so far ends the ladder, as
does running out of methods: the function then needs a creative edit and TROUBLE.md says where it stands.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from unbake import atomic as atomic_files
from unbake.config import Held, Host, Project
from unbake.search import BUILTINS


@dataclass
class Ladder:
    # method -> the compare percent after it ran
    tried: dict[str, float] = field(default_factory=dict)
    best: float = 0.0
    # method -> why it did not apply to this function
    skipped: dict[str, str] = field(default_factory=dict)
    # The method whose search or whose adopted text's compare is outstanding.
    method: str = ""

    def next_method(self) -> str | None:
        return next((name for name in BUILTINS if name not in self.tried and name not in self.skipped), None)

    def gained(self, percent: float) -> bool:
        return percent > self.best


def snapshot_path(file: Path) -> Path:
    return file.with_name(f"{file.stem}.ladder.c")


def target_assembly(project: Project, host: Host, function: str) -> str:
    """The function's own block of the naming version's extracted assembly."""
    from unbake import extract
    from unbake.decomp.draft_input import assembly_source
    from unbake.layout import split
    from unbake.work.draft import naming_version

    version = naming_version(project, split.holding_versions(project, function))
    text, _ = assembly_source(project, version, function, extract.directory(project, host, version))
    start = re.search(rf"^\s*glabel\s+{re.escape(function)}\s*$", text, re.M)
    if start is None:
        return text
    following = re.search(r"^\s*glabel\s+", text[start.end() :], re.M)
    return text[start.start() : start.end() + following.start()] if following else text[start.start() :]


def write_trouble(project: Project, host: Host, function: str, file: Path, ladder: Ladder, difference: str) -> Path:
    """build/work/FUNC/TROUBLE.md: the target, the best C so far, the first difference and what was tried."""
    try:
        assembly = target_assembly(project, host, function)
    except Held as error:
        assembly = f"unavailable: {error.reason}"
    methods = "\n".join(
        [f"| {name} | {value:.2f}% |" for name, value in ladder.tried.items()]
        + [f"| {name} | skipped: {why} |" for name, why in ladder.skipped.items()]
    )
    path = file.with_name("TROUBLE.md")
    atomic_files.text(
        path,
        f"# {function}\n\nBest compare so far: {ladder.best:.2f}%. Edit {file.name} until it is exact.\n\n"
        f"## Target assembly\n\n```\n{assembly.rstrip()}\n```\n\n"
        f"## Best C\n\n```c\n{file.read_text().rstrip()}\n```\n\n"
        f"## First difference\n\n{difference or 'none reported'}\n\n"
        f"## Methods tried\n\n| method | result |\n| --- | --- |\n{methods}\n",
        encoding="utf-8",
    )
    return path
