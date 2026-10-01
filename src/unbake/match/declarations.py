"""Preflight shared layouts and prepare final source declarations."""

from __future__ import annotations

import re
from pathlib import Path

from unbake.decomp import needs
from unbake.layout import shared, structs
from unbake.layout.split import Edit
from unbake.layout.structs_parser import Parser
from unbake.project.config import Policy, Project


def preflight(project: Project, policy: Policy, pending: list[needs.Need]) -> list[Edit]:
    """Validate shared layout needs without writes, copies, generations, or builds.

    Call after deriving trial needs and before retaining or submitting a trial.
    Missing aggregates produce proposed shared-header edits; conflicts raise Held.
    """
    return structs.resolve([need for need in pending if isinstance(need, needs.LayoutNeed)], project, policy)


def final_source(project: Project, text: str) -> str:
    """Move local aggregate definitions to their shared homes and remove draft markers."""
    records = Parser(text).parse()
    if records:
        headers = {path: path.read_text() for root in project.include for path in Path(root).rglob("*.h")}
        destinations: dict[str, Path] = {}
        combined = "\n".join(headers.values())
        parsed = Parser(combined).parse()
        cursor = 0
        for path, content in headers.items():
            for record in parsed:
                if cursor <= record.start < cursor + len(content):
                    for name in (record.name, *record.aliases):
                        destinations[name] = path
            cursor += len(content) + 1
        includes: set[str] = set()
        spans: list[tuple[int, int]] = []
        for record in records:
            destination = destinations.get(record.name, shared.home(project))
            include = next(
                destination.relative_to(root).as_posix() for root in project.include if destination.is_relative_to(root)
            )
            includes.add(include)
            start = record.start
            prefix = text[:start]
            typedef = re.search(r"\btypedef\s*$", prefix)
            if typedef:
                start = typedef.start()
            end = text.find(";", record.end)
            if end < 0:
                structs.held(record.name, "missing declaration terminator")
            spans.append((start, end + 1))
        for start, end in sorted(spans, reverse=True):
            text = text[:start] + text[end:]
        for include in sorted(includes):
            if not re.search(rf'^\s*#\s*include\s*[<"]{re.escape(include)}[>"]', text, re.M):
                text = f'#include "{include}"\n' + text
    return re.sub(r"^[ \t]*/\*\s*NON_MATCHING:\s*draft\b[^\n]*\*/[ \t]*\n?", "", text, flags=re.M)
