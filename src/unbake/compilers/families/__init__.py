"""Compiler family behavior, selected by the compiler registry."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from unbake.compilers.families.gcc.schedule import Schedule
from unbake.objects.elf import Object
from unbake.objects.rodata import Pool

if TYPE_CHECKING:
    from unbake.compilers.families.mips import Relocation, Shape
    from unbake.decomp.explain import Allocation

from unbake.config import Held


class CompilerIdentity(Protocol):
    @property
    def id(self) -> str: ...


@runtime_checkable
class Family(Protocol):
    def dependency_paths(self, output: str) -> tuple[str, ...]: ...
    def preprocess_flags(self, preprocess: tuple[str, ...], codegen: tuple[str, ...]) -> tuple[str, ...]: ...
    def analysis_flags(
        self, compiler: Path, cpp: str, root: Path, preprocess: tuple[str, ...], codegen: tuple[str, ...]
    ) -> tuple[str, ...]: ...
    def shape(self, compiler: str, cflags: tuple[str, ...]) -> Shape: ...
    def rodata_section(self) -> str: ...
    def move_idiom(self) -> str: ...
    def relocation_pairs(self, relocations: Iterable[Relocation]) -> list[tuple[Relocation | None, Relocation]]: ...
    def probe_cflags(self) -> tuple[str, ...]: ...
    def literal_pools(self, obj: Object) -> list[Pool]: ...
    def jump_tables(self, obj: Object) -> list[Pool]: ...
    def dump_flags(self) -> tuple[str, ...]: ...
    def allocation(self, dumps: Mapping[str, str]) -> Allocation: ...
    def schedule(self, dumps: Mapping[str, str | Path] | None) -> Schedule: ...


def dependency_rules(output: str) -> tuple[str, ...]:
    """Native make dependency right-hand sides, without inventing missing inputs."""
    result = []
    for line in output.replace("\\\n", " ").splitlines():
        if not line.strip():
            continue
        target, separator, names = line.partition(":")
        if not target.strip() or not separator:
            raise Held("compile", "compile.dependencies: invalid native dependency rule")
        if names.strip():
            result.append(names.strip())
    return tuple(result)


def family_for(compiler: str | CompilerIdentity) -> Family:
    from unbake.compilers.families.gcc import Gcc
    from unbake.compilers.families.ido import Ido
    from unbake.compilers.registry import registry

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
