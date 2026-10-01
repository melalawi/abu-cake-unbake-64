"""Compiler family behavior, selected by the compiler registry."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from unbake.families.gcc.schedule import Schedule
from unbake.project_tools.elf import Object
from unbake.project_tools.rodata import Pool

if TYPE_CHECKING:
    from unbake.decomp.explain import Allocation
    from unbake.families.mips import Relocation

from unbake.project.config import Held


class CompilerIdentity(Protocol):
    @property
    def id(self) -> str: ...


@runtime_checkable
class Family(Protocol):
    def rodata_section(self) -> str: ...
    def move_idiom(self) -> str: ...
    def relocation_pairs(self, relocations: Iterable[Relocation]) -> list[tuple[Relocation | None, Relocation]]: ...
    def probe_cflags(self) -> tuple[str, ...]: ...
    def literal_pools(self, obj: Object) -> list[Pool]: ...
    def jump_tables(self, obj: Object) -> list[Pool]: ...
    def dump_flags(self) -> tuple[str, ...]: ...
    def allocation(self, dumps: Mapping[str, str]) -> Allocation: ...
    def schedule(self, dumps: Mapping[str, str | Path] | None) -> Schedule: ...


def family_for(compiler: str | CompilerIdentity) -> Family:
    from unbake.families.gcc import Gcc
    from unbake.families.ido import Ido
    from unbake.project.toolchain import registry

    ident = compiler if isinstance(compiler, str) else getattr(compiler, "id", None)
    if not ident:
        raise Held("families", "compiler.id: missing value")
    specs = registry()
    if ident not in specs:
        raise Held("families", f"compiler {ident}: missing registry entry")
    family = specs[ident].family
    if family == "gcc":
        return Gcc()
    if family == "ido":
        return Ido()
    raise Held("families", f"compiler {ident}.family {family}: unsupported")
