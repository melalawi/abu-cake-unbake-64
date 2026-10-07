"""The tracked, strict schema for declaration ownership."""

from __future__ import annotations

import json
import re
import tomllib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NoReturn

from unbake import atomic as atomic_files
from unbake.config import Held, Project
from unbake.layout import split


@dataclass(frozen=True)
class Member:
    name: str
    segment: str
    address: int
    versions: tuple[str, ...]


@dataclass(frozen=True)
class Group:
    name: str
    segment: str
    evidence: str
    members: tuple[str, ...]
    only: dict[str, tuple[str, ...]] = field(default_factory=dict)
    split: tuple[str, ...] = ()
    signals: tuple[str, ...] = ()

    @property
    def header(self) -> str:
        return f"{self.segment}/{self.name}.h"


SIGNALS = ("callee", "cap", "chain", "padding", "rodata", "split", "version")
"""Evidence that formed an inferred group (layout.modules)."""


@dataclass(frozen=True)
class Map:
    cap: int
    groups: tuple[Group, ...]

    @property
    def owners(self) -> dict[str, Group]:
        return {member: group for group in self.groups for member in group.members}


def refuse(key: str, reason: str) -> NoReturn:
    raise Held("layout", f"layout.{key}: {reason}")


def positive(value: Any, key: str) -> int:
    if type(value) is not int or value < 1:
        refuse(key, "expected positive integer")
    return int(value)


def label(value: Any, key: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z_]\w*", value):
        refuse(key, f"invalid name {value!r}")
    return str(value)


def unknown(row: dict[str, Any], allowed: set[str], key: str) -> None:
    for name in sorted(row.keys() - allowed):
        refuse(f"{key}.{name}", "unknown key")


def catalog(project: Project, proposed: Mapping[Path, str] | None = None) -> dict[str, Member]:
    """Union version members; anchor addresses and segments in names_from.

    Proposed split text supplies the inventory for a validated boundary plan without writing it.
    Existing rows retain the function reader's explicit disassembler-data exclusions.
    """
    result: dict[str, Member] = {}
    order = (project.names_from, *(v for v in project.versions if v != project.names_from))
    for version in order:
        path = project.version(version).split
        owners: dict[str, Any] = {}
        original = split.layout(path)[2]
        functions = split.functions(project, version)
        entries = [(function.path, function.address) for function in functions]
        segments = original
        if proposed is not None and path in proposed:
            segments = split.parse_layout(path, proposed[path])[2]
            known = {function.path for function in functions}
            kinds = {row.path: row.kind for segment in original for row in segment.rows}
            entries = [
                (row.path, split.address(row, path))
                for segment in segments
                for row in segment.rows
                if row.kind in split.CODE_KINDS and (row.path in known or kinds.get(row.path) not in split.CODE_KINDS)
            ]
        for segment in segments:
            for row in segment.rows:
                owners.setdefault(row.path, segment)
        for entry_path, address in entries:
            segment = owners[entry_path]
            name = Path(entry_path).name
            segment_name = split.plain(segment.fields.get("name", f"span_{int(segment.fields['start'], 0):X}"))
            if name in result:
                old = result[name]
                result[name] = Member(name, old.segment, old.address, (*old.versions, version))
            else:
                result[name] = Member(name, segment_name, address, (version,))
    return result


def validate(value: dict[str, Any], versions: tuple[str, ...], members: dict[str, Member]) -> Map:
    unknown(value, {"schema", "cap", "group"}, "map")
    if type(value.get("schema")) is not int or value["schema"] != 1:
        refuse("schema", "expected 1")
    cap = positive(value.get("cap"), "cap")
    rows = value.get("group")
    if not isinstance(rows, list) or (not rows and members):
        refuse("group", "expected nonempty groups")
    groups = []
    names: set[tuple[str, str]] = set()
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            refuse("group", "expected table")
        unknown(row, {"name", "segment", "evidence", "members", "only", "split", "signals"}, "group")
        name = label(row.get("name"), "group.name")
        if name in {"types", "data"}:
            refuse(f"group.{name}", "duplicate or reserved group name")
        selected = row.get("members")
        if not isinstance(selected, list) or not selected:
            refuse(f"group.{name}.members", "empty group")
        for member in selected:
            label(member, f"group.{name}.members")
            if member in seen:
                refuse(f"member.{member}", "duplicate member")
            if member not in members:
                refuse(f"member.{member}", "unknown member")
            seen.add(member)
        segments = {members[m].segment for m in selected}
        segment = row.get("segment")
        if segment is None and len(segments) == 1:
            segment = next(iter(segments))
        segment = label(segment, f"group.{name}.segment")
        if (segment, name) in names:
            refuse(f"group.{name}", "duplicate group name in segment")
        names.add((segment, name))
        if segments != {segment}:
            refuse(f"group.{name}.segment", "members cross code segment boundaries")
        addresses = [members[m].address for m in selected]
        if addresses != sorted(addresses):
            refuse(f"group.{name}.members", "members out of address order")
        evidence = row.get("evidence")
        if evidence not in ("default", "inferred", "hypothesis", "proven"):
            refuse(f"group.{name}.evidence", "expected default, inferred, hypothesis or proven")
        signals = row.get("signals", [])
        if not isinstance(signals, list) or any(s not in SIGNALS for s in signals) or signals != sorted(set(signals)):
            refuse(f"group.{name}.signals", f"expected sorted distinct names from {', '.join(SIGNALS)}")
        only = row.get("only", {})
        if not isinstance(only, dict):
            refuse(f"group.{name}.only", "expected table")
        for member, marks in only.items():
            if member not in selected:
                refuse(f"only.{member}", "not a member of group")
            if not isinstance(marks, list) or not marks:
                refuse(f"only.{member}", "expected nonempty version list")
            for version in marks:
                if version not in versions:
                    refuse(f"only.{member}.{version}", "unknown version")
            if len(set(marks)) != len(marks):
                refuse(f"only.{member}", "duplicate version")
        cuts = row.get("split", [])
        if not isinstance(cuts, list) or any(c not in selected for c in cuts) or len(set(cuts)) != len(cuts):
            refuse(f"group.{name}.split", "expected distinct member cut points")
        groups.append(
            Group(
                name,
                segment,
                evidence,
                tuple(selected),
                {k: tuple(v) for k, v in only.items()},
                tuple(cuts),
                tuple(signals),
            )
        )
    missing = members.keys() - seen
    if missing:
        refuse(f"member.{sorted(missing)[0]}", "missing from map")
    return Map(cap, tuple(groups))


def encoded(value: Map) -> bytes:
    lines = ["schema = 1", f"cap = {value.cap}"]
    if not value.groups:
        lines.append("group = []")
    for group in value.groups:
        lines.extend(
            (
                "",
                "[[group]]",
                f"name = {json.dumps(group.name)}",
                f"segment = {json.dumps(group.segment)}",
                f"evidence = {json.dumps(group.evidence)}",
                f"members = {json.dumps(group.members)}",
            )
        )
        if group.only:
            pairs = (f"{json.dumps(k)} = {json.dumps(v)}" for k, v in group.only.items())
            lines.append("only = { " + ", ".join(pairs) + " }")
        if group.split:
            lines.append(f"split = {json.dumps(group.split)}")
        if group.signals:
            lines.append(f"signals = {json.dumps(group.signals)}")
    return ("\n".join(lines) + "\n").encode()


def load(project: Project) -> Map:
    path = project.root / "layout.toml"
    try:
        value = tomllib.loads(path.read_text())
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise Held("layout", f"layout.map: {path}: {error}") from error
    return validate(value, project.versions, catalog(project))


def stale(project: Project) -> tuple[str, ...]:
    """Names whose ownership disagrees with the current catalog, in either direction.

    The map step's key must change for unowned new rows as well as removed or renamed rows.
    """
    target = project.root / "layout.toml"
    try:
        value = tomllib.loads(target.read_text())
    except (OSError, tomllib.TOMLDecodeError):
        return ()
    members = catalog(project)
    named = {
        m
        for group in value.get("group", [])
        if isinstance(group, dict)
        for m in group.get("members", [])
        if isinstance(m, str)
    }
    return tuple(sorted(named ^ members.keys()))


def _retain(group: dict[str, Any], names: list[str]) -> None:
    """The one membership change: set a group's members; the `only` marks and `split` cuts of the members it
    removes go with them (a mark that never named a member stays, so validation names it)."""
    gone = set(group.get("members", ())) - set(names)
    group["members"] = names
    if isinstance(group.get("only"), dict):
        group["only"] = {k: v for k, v in group["only"].items() if k not in gone}
    if isinstance(group.get("split"), list):
        group["split"] = [m for m in group["split"] if m not in gone]


def _drop_stale_defaults(value: dict[str, Any], members: dict[str, Member]) -> None:
    """Drop names no split holds from `default` groups (inference plans their rows again); authored and proven
    groups keep them, so validation names the conflict."""
    if not isinstance(value.get("group"), list):
        return
    kept = []
    for group in value["group"]:
        if isinstance(group, dict) and group.get("evidence") == "default" and isinstance(group.get("members"), list):
            _retain(group, [m for m in group["members"] if m in members])
            if not group["members"]:
                continue
        kept.append(group)
    if "group" in value:
        value["group"] = kept


def ensure(project: Project) -> bool:
    """Infer default and unowned catalog members; preserve every other valid group.

    Validate existing ownership before inference, then validate complete coverage before the one write.
    """
    from unbake.layout import modules

    target = project.root / "layout.toml"
    try:
        before = target.read_bytes()
        value = tomllib.loads(before.decode("utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as error:
        raise Held("layout", f"layout.map: {target}: {error}") from error
    members = catalog(project)
    _drop_stale_defaults(value, members)
    named = {
        name
        for group in (value["group"] if isinstance(value.get("group"), list) else [])
        if isinstance(group, dict) and isinstance(group.get("members"), list)
        for name in group["members"]
        if isinstance(name, str)
    }
    # Only coverage is deferred: unknown names, duplicate owners and every authored constraint stay strict.
    current = validate(value, project.versions, {name: member for name, member in members.items() if name in named})
    if named == members.keys() and all(group.evidence != "default" for group in current.groups):
        return False
    encoded_map = encoded(modules.infer(project, current, members))
    validate(tomllib.loads(encoded_map.decode("utf-8")), project.versions, members)
    if encoded_map == before:
        return False
    atomic_files.write(target, encoded_map)
    return True


def regroup(
    value: dict[str, Any],
    members: dict[str, Member],
    versions: tuple[str, ...],
    replacements: dict[str, tuple[str, ...]],
    cuts: Iterable[str] = (),
    proven: Iterable[str] = (),
) -> Map:
    """Every membership edit of an existing layout: a member maps to its new names (a rename, a folded tail that
    stays, a moved entry) or to () (dropped); `cuts` become split cut points; a group holding a `proven` name is
    marked proven. Groups keep their authored rows and address order, `only` and `split` follow the members, and
    the result is validated, so a bad edit is refused here and never by a later load."""
    cuts, proven = set(cuts), set(proven)
    present = {name for group in value["group"] for name in group["members"]}
    for name in sorted((replacements.keys() | cuts | proven) - present):
        refuse(f"member.{name}", "edit names an absent member")
    everywhere = set(versions)
    for group in value["group"]:
        selected = list(dict.fromkeys(child for name in group["members"] for child in replacements.get(name, (name,))))
        # A replacement can move a member (a boundary edit changes its address): keep address order, stably.
        selected.sort(key=lambda name: members[name].address if name in members else 0)
        marks = {name: list(held) for name, held in group.get("only", {}).items() if name not in replacements}
        for name in group["members"]:
            for child in replacements.get(name, ()):
                if child in members and set(members[child].versions) != everywhere:
                    marks[child] = list(members[child].versions)
        group["only"] = marks
        mapped = (child for name in group.get("split", []) for child in replacements.get(name, (name,)))
        group["split"] = list(dict.fromkeys((*mapped, *(name for name in group["members"] if name in cuts))))
        if proven & set(group["members"]):
            group["evidence"] = "proven"
        _retain(group, selected)
    value["group"] = [group for group in value["group"] if group["members"]]
    return validate(value, versions, members)


def edit_members(
    project: Project,
    replacements: dict[str, tuple[str, ...]],
    *,
    cuts: Iterable[str] = (),
    proven: Iterable[str] = (),
) -> None:
    """Apply one `regroup` to layout.toml in one write."""
    from unbake.typemap import storage

    target = project.root / "layout.toml"
    value = tomllib.loads(target.read_text())
    storage.write(target, encoded(regroup(value, catalog(project), project.versions, replacements, cuts, proven)))
