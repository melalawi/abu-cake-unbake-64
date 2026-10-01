"""IDO per-object constant sections."""

from __future__ import annotations

from unbake.project.config import Held
from unbake.project_tools.elf import Object
from unbake.project_tools.rodata import Pool, pools


def literal_pools(obj: Object) -> list[Pool]:
    """Return literal ranges in .rodata, including explicit alignment bytes."""
    try:
        return pools(obj, ".rodata", False)
    except ValueError as error:
        raise Held("rodata", str(error)) from error


def jump_tables(obj: Object) -> list[Pool]:
    """Return contiguous R_MIPS_32 runs targeting this object's text."""
    try:
        return pools(obj, ".rodata", True)
    except ValueError as error:
        raise Held("rodata", str(error)) from error
