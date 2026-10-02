"""Native entry symbols and contiguous source ownership for trial and publication."""

from __future__ import annotations

import re
import subprocess
from itertools import pairwise
from pathlib import Path

from pycparser import c_ast, c_parser  # type: ignore[import-untyped]

from unbake.layout import split
from unbake.project import makefile
from unbake.project.config import Held, Policy, Project
from unbake.typemap.declarations import clean


def definitions(project: Project, policy: Policy, source: Path, version: str, text: str) -> set[str]:
    """Read active global C definitions, excluding declarations and static helpers."""
    flags = makefile.flags(project, version, project.src / source.name)
    options: list[str] = []
    pending = iter(flags)
    for flag in pending:
        if flag in ("-I", "-D", "-U", "-include", "-isystem"):
            options.extend((flag, next(pending)))
        elif flag.startswith(("-I", "-D", "-U")):
            options.append(flag)
    result = subprocess.run(
        [str(policy.cpp), *policy.cppflags, *options, "-DNON_MATCHING=1", "-x", "c", "-"],
        cwd=project.root,
        input=text,
        capture_output=True,
        text=True,
    )
    if result.returncode:
        raise Held("try", "trial.entries_source: " + result.stderr.strip())
    try:
        tree = c_parser.CParser().parse(clean(result.stdout))
    except c_parser.ParseError as error:
        raise Held("try", f"trial.entries_source: {error}") from error
    return {
        node.decl.name for node in tree.ext if isinstance(node, c_ast.FuncDef) and "static" not in node.decl.storage
    }


def owners(
    project: Project, policy: Policy, source: Path, version: str, *, text: str | None = None
) -> list[split.Function]:
    """One C item may own several adjacent text rows; all remain byte pinned."""
    rows = split.functions(project, version)
    first = [row for row in rows if source.stem in row.aliases]
    if len(first) != 1:
        raise Held("try", f"trial.entries_layout: {source.stem}: requires one owner in {version}")
    # Most sources contain just their named function. Avoid requiring a C parser
    # for those callers; compiled symbol validation remains authoritative.
    text = source.read_text() if text is None else text
    possible = set(re.findall(r"\b([A-Za-z_]\w*)\s*\([^;{}]*\)\s*\{", clean(text)))
    if not any(name != source.stem and any(name in row.aliases for row in rows) for name in possible):
        return first
    names = definitions(project, policy, source, version, text)
    selected = sorted((row for row in rows if names.intersection(row.aliases)), key=lambda row: row.start)
    if not selected or selected[0] != first[0]:
        raise Held("try", f"trial.entries_layout: {source.stem}: must name the first entry")
    _, _, segments = split.layout(project.version(version).split)
    containing = [segment for segment in segments if any(row.path == first[0].path for row in segment.rows)]
    if len(containing) != 1 or any(not any(row.path == owner.path for row in containing[0].rows) for owner in selected):
        raise Held("try", f"trial.entries_layout: {source.stem}: entries must share one segment")
    for left, right in pairwise(selected):
        if left.end != right.start or left.address + left.end - left.start != right.address or right.kind != "asm":
            raise Held("try", f"trial.entries_layout: {source.stem}: entries must own contiguous assembly")
    return selected
