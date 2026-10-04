"""The tracked, strict schema for declaration ownership."""

from __future__ import annotations

import json
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NoReturn

from unbake.layout import split
from unbake.project.config import Held, Project


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

    @property
    def header(self) -> str:
        return f"{self.segment}/{self.name}.h"


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


def catalog(project: Project) -> dict[str, Member]:
    """Union version members; anchor addresses and segments in names_from."""
    result: dict[str, Member] = {}
    order = (project.names_from, *(v for v in project.versions if v != project.names_from))
    for version in order:
        path = project.version(version).split
        segments = split.layout(path)[2]
        for function in split.functions(project, version):
            segment = next(s for s in segments if any(row.path == function.path for row in s.rows))
            name = Path(function.path).name
            segment_name = split.plain(segment.fields.get("name", f"span_{int(segment.fields['start'], 0):X}"))
            if name in result:
                old = result[name]
                result[name] = Member(name, old.segment, old.address, (*old.versions, version))
            else:
                result[name] = Member(name, segment_name, function.address, (version,))
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
        unknown(row, {"name", "segment", "evidence", "members", "only", "split"}, "group")
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
        if evidence not in ("default", "hypothesis", "proven"):
            refuse(f"group.{name}.evidence", "expected default, hypothesis or proven")
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
            Group(name, segment, evidence, tuple(selected), {k: tuple(v) for k, v in only.items()}, tuple(cuts))
        )
    missing = members.keys() - seen
    if missing:
        refuse(f"member.{sorted(missing)[0]}", "missing from map")
    return Map(cap, tuple(groups))


def default(cap: int, members: dict[str, Member], versions: tuple[str, ...]) -> Map:
    cap = positive(cap, "cap")
    segments: dict[str, list[Member]] = {}
    for member in sorted(members.values(), key=lambda m: (m.address, m.name)):
        segments.setdefault(member.segment, []).append(member)
    groups = []
    for segment, rows in segments.items():
        for offset in range(0, len(rows), cap):
            run = rows[offset : offset + cap]
            only = {m.name: m.versions for m in run if set(m.versions) != set(versions)}
            groups.append(Group(f"code_{run[0].address:08X}", segment, "default", tuple(m.name for m in run), only))
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
    return ("\n".join(lines) + "\n").encode()


def load(project: Project) -> Map:
    path = project.root / "layout.toml"
    try:
        value = tomllib.loads(path.read_text())
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise Held("layout", f"layout.map: {path}: {error}") from error
    return validate(value, project.versions, catalog(project))


def ensure(project: Project) -> None:
    target = project.root / "layout.toml"
    if target.is_file():
        with target.open("rb") as source:
            current = tomllib.load(source)
        if current.get("group") != []:
            return
    with (project.root / "config.toml").open("rb") as source:
        config = tomllib.load(source)
    cap = positive(config["project"].get("layout_cap"), "project.layout_cap")
    value = default(cap, catalog(project), project.versions)
    (project.root / "layout.toml").write_bytes(encoded(value))


def edit_members(project: Project, replacements: dict[str, tuple[str, ...]]) -> None:
    """Apply explicit member edits while retaining authored groups and cuts."""
    target = project.root / "layout.toml"
    value = tomllib.loads(target.read_text())
    members = catalog(project)
    present = {name for group in value["group"] for name in group["members"]}
    for name in replacements.keys() - present:
        refuse(f"member.{name}", "edit names an absent member")
    for group in value["group"]:
        selected = tuple(dict.fromkeys(child for name in group["members"] for child in replacements.get(name, (name,))))
        marks = {name: versions for name, versions in group.get("only", {}).items() if name not in replacements}
        for name in group["members"]:
            for child in replacements.get(name, ()):
                if child in members and set(members[child].versions) != set(project.versions):
                    marks[child] = list(members[child].versions)
        group["members"] = list(selected)
        group["only"] = marks
        group["split"] = list(
            dict.fromkeys(child for name in group.get("split", []) for child in replacements.get(name, (name,)))
        )
    value["group"] = [group for group in value["group"] if group["members"]]
    from unbake.typemap import storage

    storage.write(target, encoded(validate(value, project.versions, members)))
