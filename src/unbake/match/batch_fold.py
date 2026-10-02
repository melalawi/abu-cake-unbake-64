"""Fold a batch's sources in parallel with the result of folding them in order.

Each source folds against the headers its predecessors leave. Workers fold a
window of sources against the headers at the window's start. The merge then
walks the window in order and adopts a worker's result only when no header
applied since could have changed it: no name the source or its result spells,
no layout identity it looked up, no header path it writes. Any other source,
and any source with staged overlay headers, folds again in place.
"""

from __future__ import annotations

import re
from collections import ChainMap
from collections.abc import Sequence
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from unbake.decomp import checks, work
from unbake.layout import split
from unbake.layout.header_context import Headers
from unbake.layout.structs import Layout
from unbake.layout.structs_identity import Index, identity
from unbake.layout.structs_types import Aggregate
from unbake.match import declarations, forked, reporting
from unbake.match.common import held
from unbake.project.config import Held, Policy, Project

_WORD = re.compile(r"[A-Za-z_]\w*")
_MISSING = object()


@dataclass(frozen=True)
class Trial:
    """One source folded against a window's starting headers."""

    kind: str  # "folded", "held", or "again" when only an in-order fold can decide
    folded: declarations.Folded | None = None
    reason: str = ""
    names: frozenset[str] = frozenset()
    identities: frozenset[str] = frozenset()
    paths: frozenset[Path] = frozenset()


@dataclass
class Changes:
    """Header bindings that changed since a window's starting headers."""

    names: set[str] = field(default_factory=set)
    identities: set[str] = field(default_factory=set)
    paths: set[Path] = field(default_factory=set)
    reloaded: bool = False

    def admits(self, trial: Trial) -> bool:
        return (
            trial.kind != "again"
            and not self.reloaded
            and self.names.isdisjoint(trial.names)
            and self.identities.isdisjoint(trial.identities)
            and self.paths.isdisjoint(trial.paths)
        )

    def apply(self, headers: Headers, edits: Sequence[split.Edit]) -> None:
        """Apply edits as Headers.apply does, recording every binding they change."""
        self.paths.update(edit.path for edit in edits)
        added = [edit for edit in edits if edit.path not in headers.texts]
        if any(edit.path in headers.texts and headers.texts[edit.path] != edit.after for edit in edits):
            # An edited header reparses the whole context; nothing earlier stays provable.
            self.reloaded = True
        if self.reloaded or not added:
            headers.apply(list(edits))
            return
        keys = {key for edit in added for word in _WORD.findall(edit.after) for key in _keys(word)}
        before = {key: _binding(headers, key) for key in keys}
        sizes = _sizes(headers)
        count = len(headers.records)
        headers.apply(list(edits))
        after = {key: _binding(headers, key) for key in keys}
        grown = [now - then for now, then in zip(_sizes(headers), sizes, strict=True)]
        appeared = [
            sum(1 for key in keys if before[key][slot] is _MISSING and after[key][slot] is not _MISSING)
            for slot in range(len(grown))
        ]
        if grown != appeared:
            # A binding appeared under a name this text does not spell; trust nothing.
            self.reloaded = True
            return
        self.names.update(key.split()[-1] for key in keys if not _same(before[key], after[key]))
        self.identities.update(identity(record, headers.index.names) for record in headers.records[count:])


def fold(
    staged: Project, policy: Policy, headers: Headers, candidates: list[Any], receipts: list[str]
) -> list[tuple[Any, declarations.Folded]]:
    """Fold candidates in order into headers; returns each accepted source with its fold."""
    accepted: list[tuple[Any, declarations.Folded]] = []
    window = max(1, policy.cores * 16)
    reused = again = 0
    start = 0
    while start < len(candidates):
        changes = Changes()
        members = candidates[start : start + window]
        trials = forked.ordered(_speculate, (staged, policy, headers), members, policy.cores)
        with closing(trials):
            for candidate, (lines, trial) in zip(members, trials, strict=False):
                start += 1
                try:
                    if changes.admits(trial):
                        reused += 1
                        for line in lines:
                            reporting.learn(line)
                        folded = _adopt(headers, trial, changes)
                    else:
                        again += 1
                        folded = _fold_one(staged, policy, headers, candidate, changes)
                except Held as error:
                    receipts.append(f"HELD(submit): {candidate.function}: submit.fold: {error.reason}")
                else:
                    accepted.append((candidate, folded))
                if changes.reloaded:
                    break
    reporting.record("fold", reused=reused, folded_in_order=again, window=window)
    return accepted


def _adopt(headers: Headers, trial: Trial, changes: Changes) -> declarations.Folded:
    if trial.kind == "held":
        held(trial.reason)
    assert trial.folded is not None
    changes.apply(headers, trial.folded.headers)
    return trial.folded


def _fold_one(
    staged: Project, policy: Policy, headers: Headers, candidate: Any, changes: Changes
) -> declarations.Folded:
    """Fold one source against the current headers and adopt its edits."""
    overlay = candidate.source.parent / "overlay"
    staged_headers = [
        split.Edit(
            staged.root / relative,
            headers.texts.get(staged.root / relative, ""),
            (overlay / relative).read_text(),
            tuple(staged.versions),
        )
        for relative in sorted(work.overlay_data(staged, candidate.source)["edits"])
    ]
    if staged_headers:
        # Headers a trial staged in its overlay become part of this source's context.
        changes.apply(headers, staged_headers)
    folded = _folded(staged, policy, headers, candidate)
    changes.apply(headers, folded.headers)
    return folded


def _folded(staged: Project, policy: Policy, headers: Headers, candidate: Any) -> declarations.Folded:
    folded = declarations.fold_source(
        staged,
        policy,
        headers,
        candidate.function,
        candidate.content.decode("utf-8"),
        candidate.versions,
        prove_headers=False,
    )
    blockers = [f for f in checks.run(folded.source) if f.fakematch is None]
    if blockers:
        held("submit.source_rules: " + "; ".join(checks.message(finding) for finding in blockers))
    return folded


def _speculate(shared: tuple[Project, Policy, Headers], candidate: Any) -> Trial:
    """Fold one source in a worker against the window's starting headers."""
    staged, policy, headers = shared
    try:
        if work.overlay_data(staged, candidate.source)["edits"]:
            return Trial("again")
    except Held as error:
        # Overlay evidence is read from disk, which the fold does not change.
        return Trial("held", reason=error.reason)
    except Exception:
        return Trial("again")
    destination = staged.include[0] / "shared" / f"{candidate.function.lower()}.h"
    probe = _Probe(headers.index)
    headers.index = probe  # type: ignore[assignment]
    words = set(_WORD.findall(candidate.content.decode("utf-8", "replace")))
    try:
        folded = _folded(staged, policy, headers, candidate)
    except Held as error:
        return Trial(
            "held",
            reason=error.reason,
            names=frozenset(words | probe.names),
            identities=frozenset(probe.identities),
            paths=frozenset({destination}),
        )
    except Exception:
        return Trial("again")
    finally:
        headers.index = probe.index
    for text in (folded.source, *(edit.after for edit in folded.headers)):
        words.update(_WORD.findall(text))
    return Trial(
        "folded",
        folded=folded,
        names=frozenset(words | probe.names),
        identities=frozenset(probe.identities),
        paths=frozenset({destination, *(edit.path for edit in folded.headers)}),
    )


class _Probe:
    """The shared layout index, recording each layout identity and name a fold looks up."""

    def __init__(self, index: Index) -> None:
        self.index = index
        self.identities: set[str] = set()
        self.names: set[str] = set()

    def resolve(self, records: list[Layout], owner: str) -> dict[str, tuple[str, Layout]]:
        names = ChainMap({name: item for item in records for name in (item.name, *item.aliases)}, self.index.names)
        for item in records:
            self.identities.add(identity(item, names))
            self.names.update((f"{item.name}_{owner}", f"{item.name}_{owner}_{item.size:#x}"))
        return self.index.resolve(records, owner)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.index, name)


def _keys(word: str) -> tuple[str, ...]:
    return word, f"struct {word}", f"union {word}", f"enum {word}"


def _binding(headers: Headers, key: str) -> tuple[Any, ...]:
    return (
        headers.types.get(key, _MISSING),
        headers.defines.get(key, _MISSING),
        headers.homes.get(key, _MISSING),
        tuple(headers.locations[key]) if key in headers.locations else _MISSING,
        headers.index.names.get(key, _MISSING),
        key in headers.sdk,
        key in headers.tag_only,
    )


def _sizes(headers: Headers) -> tuple[int, ...]:
    return (
        len(headers.types),
        len(headers.defines),
        len(headers.homes),
        len(headers.locations),
        len(headers.index.names),
    )


def _same(left: Any, right: Any) -> bool:
    if left is right:
        return True
    if isinstance(left, tuple) and isinstance(right, tuple):
        return len(left) == len(right) and all(_same(a, b) for a, b in zip(left, right, strict=True))
    if isinstance(left, (Aggregate, Layout)) or isinstance(right, (Aggregate, Layout)):
        return False
    return bool(left == right)
