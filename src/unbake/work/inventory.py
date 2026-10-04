"""The function inventory of every version, grouped across versions and classified by body."""

from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path

from unbake.config import Held, Project
from unbake.layout import split


@dataclass(frozen=True)
class Row:
    function: str
    versions: tuple[str, ...]
    names: dict[str, str]
    aliases: tuple[str, ...]
    size: int
    score: float | None
    identical: bool
    draft: Path | None
    route: str
    evidence: tuple[str, ...]


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise Held("plan", f"{name}: required nonempty value")
    return value


def classify(data: bytes) -> tuple[str, str]:
    """Return a route and its word evidence; classification is a work hint."""
    if not isinstance(data, bytes) or not data or len(data) % 4:
        raise Held("plan", "words: required nonempty complete big-endian words")
    words = [item[0] for item in struct.iter_unpack(">I", data)]
    returns = [index for index, word in enumerate(words) if word == 0x03E00008]
    if len(returns) > 1:
        return "boundary", f"{len(returns)} jr-ra instructions in one interval"
    if not any(words) or words[0] == 0:
        return "boundary", "leading padding"
    if len(words) >= 2 and all(0x80000000 <= word < 0xC0000000 and word % 4 == 0 for word in words):
        return "table", "aligned address table"
    if all(byte == 0 or 32 <= byte < 127 for byte in data) and any(32 <= byte < 127 for byte in data):
        return "table", "text data in a function interval"
    if any(word >> 26 == 16 or word >> 26 == 47 for word in words):
        return "asm", "privileged instruction"
    if returns:
        end = returns[0] + 2
        if end > len(words):
            return "boundary", "jr-ra delay slot outside the interval"
        if len(words) - end >= 2 and not any(words[end:]):
            return "boundary", "padding beyond the return delay slot"
        if any(words[end:]):
            return "boundary", "words beyond the return delay slot"
        return "drafter", "one complete return"
    if any(word >> 26 == 0 and word & 63 == 8 for word in words):
        return "drafter", "indirect dispatch"
    return "merge", "no complete return or indirect dispatch"


def inventory(project: Project) -> tuple[str, list[split.Function], dict[tuple[str, str], bytes]]:
    versions = getattr(project, "versions", None)
    if not isinstance(versions, tuple) or not versions or len(set(versions)) != len(versions):
        raise Held("plan", "project.versions: required distinct VERSIONs")
    reference = _text(getattr(project, "names_from", None), "project.names_from")
    if reference not in versions:
        raise Held("plan", f"project.names_from {reference}: unknown VERSION")
    inventory = [item for version in versions for item in split.functions(project, version)]
    bodies = {(item.version, item.name): split.words(project, item) for item in inventory}
    return reference, inventory, bodies


def groups(inventory: list[split.Function], bodies: dict[tuple[str, str], bytes]) -> list[list[split.Function]]:
    """Shared names join variants; equal bytes join only unique VERSION rows."""
    parent = list(range(len(inventory)))

    def root(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def join(left: int, right: int) -> None:
        parent[root(right)] = root(left)

    aliases: dict[str, int] = {}
    equal: dict[bytes, list[int]] = {}
    for index, item in enumerate(inventory):
        for name in (item.name, *item.aliases):
            if name in aliases:
                join(index, aliases[name])
            else:
                aliases[name] = index
        equal.setdefault(bodies[item.version, item.name], []).append(index)
    for indices in equal.values():
        if len({inventory[index].version for index in indices}) != len(indices):
            continue
        for index in indices[1:]:
            left, right = root(indices[0]), root(index)
            left_versions = {item.version for offset, item in enumerate(inventory) if root(offset) == left}
            right_versions = {item.version for offset, item in enumerate(inventory) if root(offset) == right}
            if left == right or left_versions.isdisjoint(right_versions):
                join(indices[0], index)
    groups: dict[int, list[split.Function]] = {}
    for index, item in enumerate(inventory):
        groups.setdefault(root(index), []).append(item)
    for items in groups.values():
        if len({item.version for item in items}) != len(items):
            raise Held("plan", f"function {items[0].name}: ambiguous VERSION identity")
    return list(groups.values())
