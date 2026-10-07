"""GCC compiler behavior."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

from unbake.compilers.families.types import Schedule
from unbake.objects.elf import Object
from unbake.objects.rodata import Pool

if TYPE_CHECKING:
    from unbake.compilers.families.mips import Relocation, Shape
    from unbake.compilers.families.types import (
        Allocation,
        Pseudo,
        PublicHeader,
        RegisterDifference,
        RuntimeHelper,
        View,
    )
    from unbake.compilers.registry import CompilerSpec
    from unbake.config import Host, PendingProject, Project
    from unbake.process import NativeResult


class Gcc:
    def preprocessed(self, result: NativeResult) -> str:
        return result.stdout

    def diagnose(self, result: Any, context: Any) -> Any | None:
        return None

    def source_intrinsics(self) -> tuple[str, ...]:
        return ("__builtin_next_arg",)

    def public_headers(self) -> tuple[PublicHeader, ...]:
        from unbake.compilers.families.gcc.stdarg import header

        return (header(),)

    def public_defines(self) -> tuple[str, ...]:
        from unbake.compilers.families.gcc.stdarg import SELECTOR

        return ("-D" + SELECTOR + "=1",)

    def region_name(self) -> str:
        return "main"

    def recognizes_idioms(self, counts: Mapping[str, int], minimum: int, numerator: int, denominator: int) -> bool:
        total = sum(counts.values())
        return total >= minimum and counts.get(self.move_idiom(), 0) * denominator >= total * numerator

    def probe_commands(
        self, cache: Path, spec: CompilerSpec, policy: Host, project: PendingProject, work: Path, source: Path
    ) -> tuple[list[str], ...]:
        return (
            [str(cache / spec.cc), "-quiet", *spec.cflags, str(source), "-o", str(work / "probe.s")],
            [
                str(policy.mips_as),
                *project.asflags,
                *project.gnu_asflags,
                str(work / "probe.s"),
                "-o",
                str(work / "probe.o"),
            ],
        )

    def assembler_release(self) -> str | None:
        return "n64link 0.3.1 (SN ASN64 2.81 rules)\n"

    def dependency_command(self, command: list[str]) -> list[str]:
        command = [word for word in command if word not in ("-E", "-P")]
        command.insert(len(command) - 1, "-M")
        return command

    def macro_command(self, command: list[str], probe: str) -> list[str]:
        # Macro catalogue uses the host analysis provider, including for native-driver units.
        return [*command[:-1], "-dM", "-undef", "-nostdinc", probe]

    def make_preprocess(self, render: Callable[[tuple[str, ...]], str]) -> str:
        preprocess = self.native_templates()["preprocess"]
        assert preprocess is not None
        return f"{render(preprocess)} -MMD -MP -MT $(@D)/$(*F).i -MF $(@D)/$(*F).d > $(@D)/$(*F).i"

    def runtime_helpers(
        self, data: bytes, read_memory: Callable[[int, int], bytes]
    ) -> tuple[tuple[int, RuntimeHelper], ...]:
        from unbake.compilers.families.gcc.runtime import helpers

        return helpers(data, read_memory)

    def native_templates(self) -> dict[str, tuple[str, ...] | None]:
        return {
            "preprocess": ("{cpp}", "{cppflags}", "{preprocess}", "{source}"),
            "compile": ("{cc}", "-quiet", "{codegen}", "{name}.i", "-o", "{name}.s"),
            "assemble": ("{n64link}", "asn64", "--as", "{as}", "{asflags}", "{name}.s", "-o", "{name}.o"),
        }

    def uses_host_cpp(self) -> bool:
        return True

    def preserve_padding(self) -> bool:
        return True

    def assembly_flags(self, extra: tuple[str, ...]) -> tuple[str, ...]:
        return (
            "-march=vr4300",
            "-mabi=32",
            "-EB",
            "-G0",
            "--no-pad-sections",
            *(flag for flag in extra if flag != "-mips3"),
        )

    def accepts_codegen(self, flag: str) -> bool:
        import re

        return (
            flag in ("-ansi", "-fsigned-char")
            or re.fullmatch(r"-G[0-9]+|-mips[1-4]|-O[0-3s]?|-g[0-3]?", flag) is not None
        )

    def analysis_cppflags(self, flags: tuple[str, ...]) -> tuple[str, ...]:
        return flags

    def analysis_location_flags(self) -> tuple[str, ...]:
        return ("-P", "-fdebug-cpp", "-ftrack-macro-expansion=2", "-ftabstop=1")

    def token_view(self, output: str, source: str, filename: str, boundary_line: int) -> View:
        from unbake.compilers.families.gcc.token_locations import decode, source_output

        return decode(source_output(output, boundary_line), source, filename)

    def m2c_registers(self, assembly: str, flags: tuple[str, ...], function: str) -> str:
        from unbake.compilers.families.gcc.decompiler import register_pairs

        return register_pairs(assembly, flags, function)

    def m2c_section(self) -> str:
        # Input to the external decompiler, independent of emitted object selectors.
        return ".rodata"

    def schedule_available(self) -> bool:
        return True

    def compare_dump_flags(self) -> tuple[str, ...]:
        return ("-ds", "-dS", "-dR", "-dl", "-dg", "-g")

    def compare_dump_suffixes(self) -> tuple[str, ...]:
        return ("sched", "sched2", "dbr", "lreg", "greg", "lalloc", "galloc")

    def hard_register_changes(self, target: int, candidate: int) -> tuple[tuple[int, int], ...]:
        from unbake.compilers.families.mips import hard_register_changes

        return hard_register_changes(target, candidate)

    def compiler_facts(
        self, dumps: Mapping[str, str], candidate: tuple[int, ...], expanded: str, function: str = ""
    ) -> dict[str, Any]:
        from unbake.compilers.families.gcc.diagnostics import function_dump
        from unbake.compilers.families.gcc.dump_facts import decisions

        selected = {name: function_dump(text, function) for name, text in dumps.items()} if function else dumps
        return decisions(selected, candidate, expanded)

    def collect_allocation(self, project: Project, policy: Host, source: Path, version: str, work: Path) -> Allocation:
        from unbake.compilers.families.gcc.diagnostics import collect_allocation

        return collect_allocation(project, policy, source, version, work)

    def collect_schedule(self, project: Project, policy: Host, source: Path, version: str, work: Path) -> Schedule:
        from unbake.compilers.families.gcc.diagnostics import collect_schedule

        return collect_schedule(project, policy, source, version, work)

    def allocation_hints(self, difference: RegisterDifference, by_number: dict[int, Pseudo]) -> list[str]:
        from unbake.compilers.families.gcc.diagnostics import allocation_hints

        return allocation_hints(difference, by_number)

    def dependency_paths(self, output: str) -> tuple[str, ...]:
        """GCC emits make-escaped words, with optional continuation/phony rules."""
        import re

        from unbake.compilers.families import dependency_rules

        return tuple(
            re.sub(r"\\(.)", r"\1", word).replace("$$", "$")
            for rule in dependency_rules(output)
            for word in re.findall(r"(?:\\.|[^\s])+", rule)
        )

    def analysis_flags(
        self,
        compiler: Path,
        cpp: str,
        root: Path,
        preprocess: tuple[str, ...],
        codegen: tuple[str, ...],
        temporary_root: Path,
    ) -> tuple[str, ...]:
        """The build already preprocesses with this host provider."""
        return self.preprocess_flags(preprocess, codegen)

    def preprocess_flags(self, preprocess: tuple[str, ...], codegen: tuple[str, ...]) -> tuple[str, ...]:
        """Language/macro input, excluding code generation optimization."""
        return (
            *preprocess,
            *self.public_defines(),
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


def adapter() -> Gcc:
    return Gcc()
