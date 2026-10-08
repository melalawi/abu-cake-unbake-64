"""`unbake boundary`: preview or apply boundary edits; an applied edit is proved by make check (split_apply)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from unbake.config import Held, Host, Project
from unbake.layout import split, split_apply
from unbake.process import named as cause_named


@dataclass
class Outcome:
    diff: list[str]
    versions: list[str]
    lines: list[str] = field(default_factory=list)


def _finish(
    project: Project, host: Host, edits: list[split.Edit], apply: bool, extra: list[str] | None = None
) -> Outcome:
    text = split_apply.diff(edits)
    versions = sorted({v for edit in edits for v in edit.versions})
    lines = [*(extra or []), *text.splitlines()]
    if not apply:
        return Outcome(text.splitlines(), versions, [*lines, f"preview: {len(edits)} file edits; add --apply to write"])
    if not edits:
        return Outcome([], [], [*lines, "no edits"])
    proved = split_apply.apply(project, host, edits)
    if proved is None:
        return Outcome([], versions, [*lines, "no edits"])
    if not proved.ok:
        raise Held(
            cause_named(
                "layout.boundary_ops._finish",
                "boundary.proof: make check refused the edit; nothing was written: " + "; ".join(proved.lines()),
                owner="layout.boundary_ops",
                stage="boundary",
            )
        )
    return Outcome(text.splitlines(), versions, [*lines, *proved.lines()])


def interval(
    project: Project, host: Host, verb: str, subject: str, version: str, start: int, end: int, *, apply: bool
) -> Outcome:
    from unbake.layout import split_edits

    extra: list[str] = []
    if verb == "function":
        edits = split_edits.cut(project, version, subject, start, end)
    elif verb == "data":
        edits = split_edits.data_cut(project, version, subject, start, end)
    elif verb == "code-in-data":
        from unbake.layout.code_interval import prove

        edits = split_edits.code(project, version, subject, start, end, policy=host)
        extra.append("proved code: " + json.dumps(prove(project, version, start, end, host), sort_keys=True))
    else:
        raise Held(
            cause_named(
                "boundary.verb", f"boundary.verb: {verb}: unknown", owner="layout.boundary_ops", stage="boundary"
            )
        )
    return _finish(project, host, edits, apply, extra)


def import_file(project: Project, host: Host, path: Path, *, apply: bool) -> Outcome:
    from unbake.layout import boundary_map

    changes = boundary_map.read(path)
    edits = boundary_map.plan(project, changes)
    if not apply:
        return _finish(project, host, edits, False, [f"{len(changes)} boundary changes"])
    proved = boundary_map.apply(project, host, changes)
    text = split_apply.diff(edits)
    versions = sorted({v for edit in edits for v in edit.versions})
    return Outcome(text.splitlines(), versions, proved.lines() if proved is not None else ["no edits"])


def prelude(project: Project, host: Host, names: list[str], versions: list[str], *, apply: bool) -> Outcome:
    """Split dead leading bytes, jump thunks and stubs ahead of a frame opening into their own units."""
    from unbake.layout import dead_prelude

    found = dead_prelude.select(dead_prelude.census(project, versions or None), names, versions)
    extra = [
        f"{item.version} {item.name}: {item.size} B {item.shape} before 0x{item.new_address:08X} "
        f"({item.function_size} B unit; {', '.join(item.references)})"
        for item in found
    ]
    return _finish(project, host, dead_prelude.plan(project, found), apply, extra)


def merge(project: Project, host: Host, names: list[str], versions: list[str], *, apply: bool) -> Outcome:
    """Join fragments that a split left behind back into the unit before them and drop their symbols."""
    from unbake.layout import fragment_merge

    found = fragment_merge.select(fragment_merge.census(project, versions or None), names, versions)
    extra = [
        f"{item.version} {item.parent}: {item.size} B + "
        + ", ".join(f"{piece.name} ({piece.size} B {piece.kind})" for piece in item.fragments)
        for item in found
    ]
    return _finish(project, host, fragment_merge.plan(project, found), apply, extra)


def same_symbol(project: Project, host: Host, path: Path, *, apply: bool) -> Outcome:
    from unbake.layout import symbol_join

    lines = symbol_join.run(project, host, path, apply=apply)
    return Outcome([], list(project.versions), list(lines))


def name_data(
    project: Project,
    host: Host,
    name: str,
    version: str | None,
    address: int | None,
    rename_from: str | None,
    all_versions: bool,
    *,
    apply: bool,
) -> Outcome:
    if all_versions:
        from unbake.layout.data_symbols import correspondence

        edits = correspondence(project, host, name)
    else:
        from unbake.decomp.symbols_edits import data_symbol

        assert version is not None and address is not None
        edits = data_symbol(project, host, version, name, address, rename_from)
    return _finish(project, host, edits, apply)
