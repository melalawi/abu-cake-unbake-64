"""C layout records, project preprocessing, and registered layout evidence."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn, cast

from unbake.config import Held
from unbake.decomp.needs import LayoutNeed, Need, register_resolver
from unbake.layout.split import Edit
from unbake.process import named as cause_named


@dataclass(frozen=True)
class Field:
    name: str
    type: str
    offset: int
    size: int
    extent: tuple[int, ...]
    declaration: str
    fields: tuple[Field, ...] = ()
    start: int = 0
    end: int = 0
    bit_offset: int | None = None
    bit_size: int | None = None


@dataclass(frozen=True)
class Layout:
    name: str
    kind: str
    fields: tuple[Field, ...]
    size: int
    alignment: int
    aliases: tuple[str, ...]
    source: str
    start: int
    end: int
    body_start: int
    body_end: int


def held(name: str, reason: str) -> NoReturn:
    raise Held(cause_named(f"{name}", f"{name}: {reason}", owner="layout.structs", stage="structs"))


def preprocess(source: Path, project: Any, policy: Any, version: str) -> str:
    """Preprocess a file with explicit project includes, flags and VERSION macros."""
    source = Path(source)
    if not source.is_file():
        held(str(source), "missing source")
    if not project.include:
        held("project.include", "missing include directories")
    if not policy.cpp:
        held("policy.cpp", "missing executable")
    from unbake.compilers import drivers
    from unbake.process import run_tool

    return run_tool(
        drivers.preprocess_command(project, str(policy.cpp), version, source.stem, source.resolve(), non_matching=True),
        project.root,
        "structs",
        context={"source": str(source), "version": version},
    )


def layouts(source: str | Path, *, project: Any = None, policy: Any = None, version: str | None = None) -> list[Layout]:
    """Parse aggregate layouts; files use explicit project preprocessing facts."""
    if isinstance(source, Path):
        for name, value in (("project", project), ("policy", policy), ("VERSION", version)):
            if value is None:
                held(name, "required for file preprocessing")
        source = preprocess(source, project, policy, cast(str, version))
    if not isinstance(source, str):
        held("source", "C text or Path required")
    if re.search(r"^\s*#\s*(include|if|ifdef|ifndef|elif)\b", source, re.M):
        held("source preprocessing", "conditional declarations and includes require project preprocessing")
    from unbake import cdecl

    return cdecl.records(source)


def resolve(pending: list[Need], project: Any, policy: Any) -> list[Edit]:
    """Fold proved layouts into shared headers before rebuilding includers."""

    def member(row: dict[str, Any]) -> Field:
        for key in Field.__dataclass_fields__:
            if key not in row and key not in ("bit_offset", "bit_size"):
                held(f"LayoutNeed.fields.{key}", "missing value")
        return Field(**{**row, "extent": tuple(row["extent"]), "fields": tuple(member(item) for item in row["fields"])})

    records = []
    for need in pending:
        if not isinstance(need, LayoutNeed):
            held("need", "LayoutNeed required")
        project.version(need.version)
        if not isinstance(need.evidence, dict):
            held(need.struct, "layout evidence required")
        for key in ("kind", "size", "alignment", "aliases"):
            if key not in need.evidence:
                held(f"{need.struct}.{key}", "missing value")
        records.append(
            Layout(
                need.struct,
                need.evidence["kind"],
                tuple(member(item) for item in cast(list[dict[str, Any]], need.fields)),
                need.evidence["size"],
                need.evidence["alignment"],
                tuple(need.evidence["aliases"]),
                need.source,
                0,
                0,
                0,
                0,
            )
        )
    from unbake.layout.structs_fold import fold

    return fold(records, project, host=policy) if records else []


register_resolver(LayoutNeed, 30, resolve)
