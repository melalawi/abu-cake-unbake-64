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
        reason = not_c(words)
        if reason:
            return "dead", reason
        return "drafter", "one complete return"
    if any(word >> 26 == 0 and word & 63 == 8 for word in words):
        return "drafter", "indirect dispatch"
    return "merge", "no complete return or indirect dispatch"


_SAVED = frozenset({16, 17, 18, 19, 20, 21, 22, 23, 30})


def _registers(word: int) -> tuple[set[int], set[int]]:
    """General registers an instruction reads and writes (MIPS III integer subset; others read none)."""
    op, rs, rt, rd = word >> 26, (word >> 21) & 31, (word >> 16) & 31, (word >> 11) & 31
    if op == 0:
        function = word & 63
        if function in (8, 9):
            return {rs}, {rd} if function == 9 else set()
        if function in (0, 2, 3):
            return {rt}, {rd}
        if function in (16, 18):
            return set(), {rd}
        if function in (17, 19, 24, 25, 26, 27):
            return {rs, rt} if function >= 24 else {rs}, set()
        return {rs, rt}, {rd}
    if op == 3:
        return set(), {31}
    if op == 15:
        return set(), {rt}
    if op in (1, 6, 7, 49, 53, 57, 61):
        return {rs}, set()
    if op in (4, 5, 40, 41, 42, 43, 46, 63):
        return {rs, rt}, set()
    if 8 <= op <= 14 or 32 <= op <= 39 or op == 55:
        return {rs}, {rt}
    return set(), set()


def not_c(words: list[int]) -> str | None:
    """Why a body with one complete return cannot be compiled C, or None. Each rule is one no compiler breaks:
    a stack frame allocated without a release (or released without an allocation), a call without saving ra,
    and a callee-saved register read before anything sets or saves it."""
    adjusts = [((word & 0xFFFF) ^ 0x8000) - 0x8000 for word in words if word >> 16 == 0x27BD]
    if any(value < 0 for value in adjusts) != any(value > 0 for value in adjusts):
        return "stack frame allocated or released but not both"
    calls = any(word >> 26 == 3 or (word >> 26 == 0 and word & 63 == 9) for word in words)
    if calls and not any(word >> 16 == 0xAFBF for word in words):
        return "call without saving ra"
    saved = {(word >> 16) & 31 for word in words if word >> 21 == (43 << 5 | 29)}
    written: set[int] = set()
    for word in words:
        reads, writes = _registers(word)
        unset = (reads & _SAVED) - written - saved
        if unset:
            return f"reads callee-saved ${min(unset)} before setting it"
        written |= writes
    return None


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

    versions = [{item.version} for item in inventory]

    def join(left: int, right: int) -> None:
        left, right = root(left), root(right)
        if left != right:
            parent[right] = left
            versions[left] |= versions[right]

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
            if left != right and versions[left].isdisjoint(versions[right]):
                join(left, right)
    groups: dict[int, list[split.Function]] = {}
    for index, item in enumerate(inventory):
        groups.setdefault(root(index), []).append(item)
    for items in groups.values():
        if len({item.version for item in items}) != len(items):
            raise Held("plan", f"function {items[0].name}: ambiguous VERSION identity")
    return list(groups.values())
