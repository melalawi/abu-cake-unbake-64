"""IDO per-object constant sections."""

from __future__ import annotations

from unbake.config import Held
from unbake.objects.elf import Object
from unbake.objects.rodata import Pool, pools
from unbake.process import capture
from unbake.process import named as cause_named


def literal_pools(obj: Object) -> list[Pool]:
    """Return literal ranges in .rodata, including explicit alignment bytes."""
    try:
        return pools(obj, ".rodata", False)
    except ValueError as error:
        raise Held(
            capture(
                error,
                cause=cause_named(
                    "compilers.families.ido.rodata.literal_pools",
                    str(error),
                    owner="compilers.families.ido.rodata",
                    stage="rodata",
                ),
            )
        ) from error


def jump_tables(obj: Object) -> list[Pool]:
    """Return contiguous R_MIPS_32 runs targeting this object's text."""
    try:
        return pools(obj, ".rodata", True)
    except ValueError as error:
        raise Held(
            capture(
                error,
                cause=cause_named(
                    "compilers.families.ido.rodata.jump_tables",
                    str(error),
                    owner="compilers.families.ido.rodata",
                    stage="rodata",
                ),
            )
        ) from error
