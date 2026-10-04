"""Rank unfinished functions, find matched relatives and deal ledger claims."""

from __future__ import annotations

import hashlib
import re
import struct
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from unbake.decomp import assign as assignments
from unbake.decomp import drafts
from unbake.decomp.score import percent
from unbake.layout import split
from unbake.config import Held, Host, Project


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


def _best(
    store: drafts.Store,
    history: dict[str, list[drafts.TrialRecord]],
    aliases: Sequence[str],
    versions: Sequence[str],
) -> tuple[float | None, bool, Path | None]:
    candidates = []
    for function in aliases:
        latest: dict[str, drafts.TrialRecord] = {}
        for row in history.get(function, ()):
            digest = row.get("source_sha256")
            if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise Held("plan", f"draft {function}: source_sha256")
            latest[digest] = row
        for digest, row in latest.items():
            label = f"draft {function} {digest}"
            scores = row.get("score")
            if not isinstance(scores, dict) or not scores:
                raise Held("plan", f"{label}: score")
            for version in versions:
                if version not in scores:
                    raise Held("plan", f"{label}: score.{version} is missing")
            values = {version: percent(value, f"{label}: score.{version}", "plan") for version, value in scores.items()}
            identical = row.get("identical_everywhere")
            if not isinstance(identical, bool):
                raise Held("plan", f"{label}: identical_everywhere")
            comparisons = row.get("compares")
            if not isinstance(comparisons, dict):
                raise Held("plan", f"{label}: compares")
            for version in versions:
                comparison = comparisons.get(version)
                if not isinstance(comparison, dict):
                    raise Held("plan", f"{label}: compares.{version}")
                for name in ("identical", "of"):
                    value = comparison.get(name)
                    if type(value) is not int or value < (1 if name == "of" else 0):
                        raise Held("plan", f"{label}: compares.{version}.{name}")
                if comparison["identical"] > comparison["of"]:
                    raise Held("plan", f"{label}: compares.{version}.identical exceeds of")
                typed = comparison.get("typed")
                if not isinstance(typed, dict):
                    raise Held("plan", f"{label}: compares.{version}.typed")
                for name in ("register", "order", "immediate", "relocation", "inserted", "missing", "changed"):
                    if type(typed.get(name)) is not int or typed[name] < 0:
                        raise Held("plan", f"{label}: compares.{version}.typed.{name}")
                if identical and any(typed.values()):
                    raise Held("plan", f"{label}: identical_everywhere disagrees with compares.{version}.typed")
                if identical and comparison["identical"] != comparison["of"]:
                    raise Held("plan", f"{label}: identical_everywhere disagrees with compares.{version}")
            candidates.append((drafts.rank(row), digest, function, min(values[version] for version in versions)))
    if not candidates:
        return None, False, None
    quality, digest, function, score = min(candidates)
    path = store.root / digest / f"{function}.c"
    try:
        content = path.read_bytes()
    except OSError as error:
        raise Held("plan", f"draft {function} source {path}: {error}") from error
    if hashlib.sha256(content).hexdigest() != digest:
        raise Held("plan", f"draft {function}: source_sha256 differs from {path}")
    return score, not quality[0], path


def ranked(project: Project, policy: Host) -> list[Row]:
    """Identical retained drafts first, then weakest fuzzy score, then size."""
    _, functions, bodies = inventory(project)
    store = drafts.Store(policy, project)
    history: dict[str, list[drafts.TrialRecord]] = {}
    for trial in store.history():
        function = _text(trial.get("function"), "draft.function")
        history.setdefault(function, []).append(trial)
    output = []
    priorities = {"boundary": 0, "table": 1, "asm": 2, "merge": 3, "drafter": 4}
    for items in groups(functions, bodies):
        if all(item.kind == "c" for item in items):
            continue
        canonical = min(items, key=lambda item: project.versions.index(item.version))
        aliases = tuple(sorted({name for item in items for name in (item.name, *item.aliases)}))
        versions = tuple(item.version for item in items)
        score, identical, draft = _best(store, history, aliases, versions)
        classifications = [classify(bodies[item.version, item.name]) for item in items]
        route = min((route for route, evidence in classifications), key=priorities.__getitem__)
        if any(item.kind == "c" for item in items):
            route = "port"
        output.append(
            Row(
                canonical.name,
                versions,
                {item.version: item.name for item in items},
                aliases,
                canonical.end - canonical.start,
                score,
                identical,
                draft,
                route,
                tuple(
                    f"{item.version}: {evidence}" for item, (_, evidence) in zip(items, classifications, strict=False)
                ),
            )
        )
    return sorted(
        output,
        key=lambda row: (
            not row.identical,
            row.score is None,
            -row.score if row.score is not None else 0,
            row.size,
            row.function,
        ),
    )


def _occupied(ledger: assignments.Ledger) -> set[str]:
    names = set()
    for row in ledger.open():
        names.add(row["function"])
        names.update(row["names"].values())
    return names


def assign(
    project: Project, policy: Host, holder: str, tier: str, *, count: int
) -> list[assignments.AssignmentRecord]:
    """Deal in ranking order; each named claim is atomic in the ledger API."""
    holder, tier = _text(holder, "holder"), _text(tier, "tier")
    if type(count) is not int or count <= 0:
        raise Held("plan", "count: required positive integer")
    rows = ranked(project, policy)
    ledger = assignments.Ledger(project, policy)
    occupied = _occupied(ledger)
    records = []
    for row in rows:
        if occupied.intersection(row.aliases):
            continue
        try:
            claimed = ledger.assign(holder, tier, function=row.function)
        except Held:
            occupied = _occupied(ledger)
            if occupied.intersection(row.aliases):
                continue
            raise
        records.extend(claimed)
        occupied.update(row.aliases)
        if len(records) == count:
            break
    if not records:
        raise Held("plan", "count: no unassigned unmatched functions")
    return records


def actionable(project: Project, policy: Host) -> list[Row]:
    """Keep supported naming-version functions whose unheld owners are assembly."""
    occupied = _occupied(assignments.Ledger(project, policy))
    output = []
    for row in ranked(project, policy):
        if row.route != "drafter" or occupied.intersection(row.aliases):
            continue
        if any(row.names[v] != row.function for v in row.versions):
            continue
        if project.compiler_for(project.src / (row.function + ".c")).kind not in ("sn64", "ido"):
            continue
        output.append(row)
    return output
