"""IDO compiler behavior."""

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


class Ido:
    def preprocess_flags(self, preprocess: tuple[str, ...], codegen: tuple[str, ...]) -> tuple[str, ...]:
        """Language/macro input, excluding code generation optimization."""
        for flag in codegen:
            if not (
                flag.startswith(("-O", "-G", "-g", "-mips"))
                or flag in ("-non_shared", "-shared", "-ansi", "-signed", "-unsigned", "-Xc", "-Xansi")
            ):
                from unbake.config import Held

                raise Held("compile", f"compile.flags: {flag}: unsupported by the ido driver")
        return (*preprocess, *(flag for flag in codegen if not flag.startswith(("-O", "-g"))))

    def shape(self, compiler: str, cflags: tuple[str, ...]) -> Shape:
        """O32; objects align .text to 16 bytes; bodies up to 64 bytes are judged as fragments.

        Original asm (probe: IDO 5.3 and 7.1 at -mips2): IDO emits no COP0 or cache instruction and never
        allocates k0/k1; it touches FCSR only with cfc1/ctc1 $31 around a cvt.w.* conversion. So `cop0`, `fcsr`,
        `kreg` and `isa` hold."""
        from unbake.compilers.families.mips import o32_shape

        return o32_shape(compiler, cflags, object_alignment=16, fragment_bytes=64, likely_copies=True)

    def rodata_section(self) -> str:
        return ".rodata"

    def move_idiom(self) -> str:
        return "or"

    def probe_cflags(self) -> tuple[str, ...]:
        return ("-O2", "-G0", "-non_shared", "-mips2")

    def relocation_pairs(self, relocations: Iterable[Relocation]) -> list[tuple[Relocation | None, Relocation]]:
        from unbake.compilers.families.mips import relocation_pairs

        return relocation_pairs(relocations)

    def literal_pools(self, obj: Object) -> list[Pool]:
        from unbake.compilers.families.ido.rodata import literal_pools

        return literal_pools(obj)

    def jump_tables(self, obj: Object) -> list[Pool]:
        from unbake.compilers.families.ido.rodata import jump_tables

        return jump_tables(obj)

    def dump_flags(self) -> tuple[str, ...]:
        from unbake.compilers.families.ido.allocation import dump_flags

        return dump_flags()

    def allocation(self, dumps: Mapping[str, str]) -> Allocation:
        from unbake.compilers.families.ido.allocation import allocation

        return allocation(dumps)

    def schedule(self, dumps: Mapping[str, str | Path] | None) -> Schedule:
        from unbake.compilers.families.ido.schedule import schedule

        return schedule(dumps)
