"""Explicit scheduling-evidence capability for IDO."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from unbake.compilers.families.gcc.schedule import Schedule


def schedule(dumps: Mapping[str, str | Path] | None) -> Schedule:
    """IDO has no scheduling dump reader; search must measure every candidate."""
    if dumps is not None:
        from unbake.config import Held

        raise Held("schedule", "dumps: IDO scheduling evidence is unsupported; supply None")
    return Schedule("ido", False, (), (), (), "IDO scheduling dumps are unavailable")
