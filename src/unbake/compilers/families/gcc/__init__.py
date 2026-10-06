"""GCC compiler behavior."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING

from unbake.compilers.families.gcc.schedule import Schedule
from unbake.objects.elf import Object
from unbake.objects.rodata import Pool

if TYPE_CHECKING:
    from unbake.compilers.families.mips import Relocation, Shape
    from unbake.decomp.explain import Allocation


class Gcc:
    def analysis_flags(
        self, compiler: Path, cpp: str, root: Path, preprocess: tuple[str, ...], codegen: tuple[str, ...]
    ) -> tuple[str, ...]:
        """The build already preprocesses with this host provider."""
        return self.preprocess_flags(preprocess, codegen)

    def preprocess_flags(self, preprocess: tuple[str, ...], codegen: tuple[str, ...]) -> tuple[str, ...]:
        """Language/macro input, excluding code generation optimization."""
        return (
            *preprocess,
            *(
                flag
                for flag in codegen
                if flag in ("-funsigned-char", "-fsigned-char", "-ansi") or flag.startswith("-std=")
            ),
        )

    def shape(self, compiler: str, cflags: tuple[str, ...]) -> Shape:
        """O32; objects align .text to 16 bytes; bodies up to 64 bytes are judged as fragments.

        Original asm (probe: KMC 2.7.2 and SN64 2.8.1 at the games' flags, KMC at -mips1): GCC emits no COP0 or
        cache instruction and never allocates k0/k1 (fixed registers); float-to-int is `trunc.w.*`, which gas
        expands with cfc1/ctc1 $31 only at -mips1, beside the conversion. So `cop0`, `fcsr`, `kreg` and `isa` hold."""
        from unbake.compilers.families.mips import o32_shape

        return o32_shape(compiler, cflags, object_alignment=16, fragment_bytes=64, likely_copies=False)

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
