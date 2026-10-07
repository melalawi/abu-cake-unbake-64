"""Native entry symbols and contiguous source ownership for trial and publication."""

from __future__ import annotations

import re
from itertools import pairwise
from pathlib import Path

from pycparser import c_ast  # type: ignore[import-untyped]

from unbake import cdecl
from unbake.compilers import drivers
from unbake.config import Held, Host, Project
from unbake.layout import split
from unbake.process import capture
from unbake.process import named as cause_named
from unbake.typemap.declarations import clean


def definitions(project: Project, policy: Host, source: Path, version: str, text: str) -> set[str]:
    """Read active global C definitions, excluding declarations and static helpers."""
    expanded = drivers.preprocess_text(project, str(policy.cpp), version, source.stem, text, "try")
    try:
        tree = cdecl.parse(clean(expanded))
    except cdecl.ParseError as error:
        raise Held(
            capture(
                error,
                cause=cause_named(
                    "trial.entries_source", f"trial.entries_source: {error}", owner="layout.entries", stage="try"
                ),
            )
        ) from error
    return {
        node.decl.name for node in tree.ext if isinstance(node, c_ast.FuncDef) and "static" not in node.decl.storage
    }


def owners(
    project: Project, policy: Host, source: Path, version: str, *, text: str | None = None
) -> list[split.Function]:
    """One C item may own several adjacent text rows; all remain byte pinned."""
    index = split.owners_by_alias(project, version)
    first = index.get(source.stem, [])
    if len(first) != 1:
        raise Held(
            cause_named(
                "trial.entries_layout",
                f"trial.entries_layout: {source.stem}: requires one owner in {version}",
                owner="layout.entries",
                stage="try",
            )
        )
    # Most sources contain just their named function. Avoid requiring a C parser
    # for those callers; compiled symbol validation remains authoritative.
    text = source.read_text() if text is None else text
    possible = set(re.findall(r"\b([A-Za-z_]\w*)\s*\([^;{}]*\)\s*\{", clean(text)))
    if not any(name != source.stem and name in index for name in possible):
        return list(first)
    names = definitions(project, policy, source, version, text)
    found = {id(row): row for name in names for row in index.get(name, [])}
    selected = sorted(found.values(), key=lambda row: row.start)
    if not selected or selected[0] != first[0]:
        raise Held(
            cause_named(
                "trial.entries_layout",
                f"trial.entries_layout: {source.stem}: must name the first entry",
                owner="layout.entries",
                stage="try",
            )
        )
    _, _, segments = split.layout(project.version(version).split)
    containing = [segment for segment in segments if any(row.path == first[0].path for row in segment.rows)]
    if len(containing) != 1 or any(not any(row.path == owner.path for row in containing[0].rows) for owner in selected):
        raise Held(
            cause_named(
                "trial.entries_layout",
                f"trial.entries_layout: {source.stem}: entries must share one segment",
                owner="layout.entries",
                stage="try",
            )
        )
    for left, right in pairwise(selected):
        if left.end != right.start or left.address + left.end - left.start != right.address or right.kind != "asm":
            raise Held(
                cause_named(
                    "trial.entries_layout",
                    f"trial.entries_layout: {source.stem}: entries must own contiguous assembly",
                    owner="layout.entries",
                    stage="try",
                )
            )
    return selected
