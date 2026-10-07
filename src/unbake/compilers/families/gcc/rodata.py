"""GCC and SN64 constant sections."""

from __future__ import annotations

from unbake.config import Held
from unbake.objects.elf import Object
from unbake.objects.rodata import Pool, pools
from unbake.process import capture
from unbake.process import named as cause_named


def literal_pools(obj: Object) -> list[Pool]:
    """Return literal ranges in .rdata, including explicit alignment bytes."""
    try:
        return pools(obj, ".rdata", False)
    except ValueError as error:
        raise Held(
            capture(
                error,
                cause=cause_named(
                    "compilers.families.gcc.rodata.literal_pools",
                    str(error),
                    owner="compilers.families.gcc.rodata",
                    stage="rodata",
                ),
            )
        ) from error


def jump_tables(obj: Object) -> list[Pool]:
    """Return contiguous R_MIPS_32 runs targeting this object's text."""
    try:
        return pools(obj, ".rdata", True)
    except ValueError as error:
        raise Held(
            capture(
                error,
                cause=cause_named(
                    "compilers.families.gcc.rodata.jump_tables",
                    str(error),
                    owner="compilers.families.gcc.rodata",
                    stage="rodata",
                ),
            )
        ) from error
