"""The function inventory of every version, grouped across versions (bodies are classified by work.shape)."""

from __future__ import annotations

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
