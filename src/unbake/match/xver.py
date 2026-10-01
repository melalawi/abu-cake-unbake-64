"""Expand a proved match to byte-identical placements in other cartridges."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

from unbake.decomp.needs import PlacementNeed
from unbake.layout import xver
from unbake.match.common import Draft, read
from unbake.project.config import Held, Project
from unbake.project.rom import normalise


def expand(project: Project, draft: Draft, receipts: list[str]) -> Draft:
    """Reuse placement evidence, accepting only exact instruction bytes.

    Every added cartridge still has to pass the staged full-ROM comparison.
    A named placement with different bytes is reported and left untouched.
    """
    if set(draft.versions) == set(project.versions):
        return draft
    reference = project.names_from
    if reference not in draft.versions:
        reference = draft.versions[0]
    selected = list(draft.versions)
    pending = list(draft.needs)
    trial = SimpleNamespace(function=draft.function, needs=[])
    for version in project.versions:
        if version in selected:
            continue
        view = replace(project, names_from=reference, versions=(reference, version))
        try:
            spans = xver.locate(view, draft.function)
            origin, target = spans[reference], spans[version]
            if origin is None or target is None:
                continue
            expected = xver.body(
                normalise(read(project.version(reference).baserom)), origin.start, origin.end, draft.function
            )
            actual = xver.body(
                normalise(read(project.version(version).baserom)), target.start, target.end, draft.function
            )
            if actual != expected:
                raise Held("match", f"VERSION {version} function {draft.function}: bytes differ")
            placements = xver.needs(view, draft.function, trial)
            pending.extend(need for need in placements if need.version == version and need not in pending)
            selected.append(version)
        except Held as error:
            receipts.append(f"HELD(match): {draft.function}: VERSION {version}: {error.reason}")
    versions = tuple(version for version in project.versions if version in selected)
    pending = [need for need in pending if not isinstance(need, PlacementNeed) or need.version in versions]
    return replace(draft, versions=versions, needs=pending)
