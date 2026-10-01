"""IDO compiler behavior."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING

from unbake.families.gcc.schedule import Schedule
from unbake.project_tools.elf import Object
from unbake.project_tools.rodata import Pool

if TYPE_CHECKING:
    from unbake.decomp.explain import Allocation
    from unbake.decomp.symbols import Relocation


class Ido:
    def rodata_section(self) -> str:
        return ".rodata"

    def move_idiom(self) -> str:
        return "or"

    def probe_cflags(self) -> tuple[str, ...]:
        return ("-O2", "-G0", "-non_shared", "-mips2")

    def relocation_pairs(self, relocations: Iterable[Relocation]) -> list[tuple[Relocation | None, Relocation]]:
        from unbake.families.mips import relocation_pairs

        return relocation_pairs(relocations)

    def literal_pools(self, obj: Object) -> list[Pool]:
        from unbake.families.ido.rodata import literal_pools

        return literal_pools(obj)

    def jump_tables(self, obj: Object) -> list[Pool]:
        from unbake.families.ido.rodata import jump_tables

        return jump_tables(obj)

    def dump_flags(self) -> tuple[str, ...]:
        from unbake.families.ido.allocation import dump_flags

        return dump_flags()

    def allocation(self, dumps: Mapping[str, str]) -> Allocation:
        from unbake.families.ido.allocation import allocation

        return allocation(dumps)

    def schedule(self, dumps: Mapping[str, str | Path] | None) -> Schedule:
        from unbake.families.ido.schedule import schedule

        return schedule(dumps)
