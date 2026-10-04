"""GCC compiler behavior."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING

from unbake.compilers.families.gcc.schedule import Schedule
from unbake.project_tools.elf import Object
from unbake.project_tools.rodata import Pool

if TYPE_CHECKING:
    from unbake.decomp.explain import Allocation
    from unbake.compilers.families.mips import Relocation


class Gcc:
    def rodata_section(self) -> str:
        return ".rdata"

    def move_idiom(self) -> str:
        return "addu"

    def probe_cflags(self) -> tuple[str, ...]:
        return ("-O2", "-G0", "-mips3")

    def relocation_pairs(self, relocations: Iterable[Relocation]) -> list[tuple[Relocation | None, Relocation]]:
        from unbake.compilers.families.mips import relocation_pairs

        return relocation_pairs(relocations)

    def literal_pools(self, obj: Object) -> list[Pool]:
        from unbake.compilers.families.gcc.rodata import literal_pools

        return literal_pools(obj)

    def jump_tables(self, obj: Object) -> list[Pool]:
        from unbake.compilers.families.gcc.rodata import jump_tables

        return jump_tables(obj)

    def dump_flags(self) -> tuple[str, ...]:
        from unbake.compilers.families.gcc.allocation import dump_flags

        return dump_flags()

    def allocation(self, dumps: Mapping[str, str]) -> Allocation:
        from unbake.compilers.families.gcc.allocation import allocation

        return allocation(dumps)

    def schedule(self, dumps: Mapping[str, str | Path] | None) -> Schedule:
        from unbake.compilers.families.gcc.schedule import schedule

        return schedule(dumps)
