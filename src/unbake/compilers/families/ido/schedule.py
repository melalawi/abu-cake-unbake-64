"""Explicit scheduling-evidence capability for IDO."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from unbake.compilers.families.types import Schedule
from unbake.process import named as cause_named


def schedule(dumps: Mapping[str, str | Path] | None) -> Schedule:
    """IDO has no scheduling dump reader; search must measure every candidate."""
    if dumps is not None:
        from unbake.config import Held

        raise Held(
            cause_named(
                "dumps",
                "dumps: IDO scheduling evidence is unsupported; supply None",
                owner="compilers.families.ido.schedule",
                stage="schedule",
            )
        )
    return Schedule("ido", False, (), (), (), "IDO scheduling dumps are unavailable")
