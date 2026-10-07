"""IDO compiler behavior."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

from unbake import cache as retention
from unbake.compilers.families.types import Schedule
from unbake.objects.elf import Object
from unbake.objects.rodata import Pool
from unbake.process import Fault
from unbake.process import named as cause_named

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


class Ido:
    def compare_dump_flags(self) -> tuple[str, ...]:
        return ()

    def compare_dump_suffixes(self) -> tuple[str, ...]:
        return ()

    def hard_register_changes(self, target: int, candidate: int) -> tuple[tuple[int, int], ...]:
        from unbake.compilers.families.mips import hard_register_changes

        return hard_register_changes(target, candidate)

    def compiler_facts(
        self, dumps: Mapping[str, str], candidate: tuple[int, ...], expanded: str, function: str = ""
    ) -> dict[str, Any]:
        return {
            "available": False,
            "family": "ido",
            "reason": "unsupported: IDO scheduler and allocator decision dumps",
        }

    def preprocessed(self, result: NativeResult) -> str:
        from unbake.compilers.families.ido.directives import preprocessed

        return preprocessed(result)

    def diagnose(self, result: Any, context: Any) -> Any | None:
        return None

    def source_intrinsics(self) -> tuple[str, ...]:
        return ("__builtin_classof", "__builtin_alignof")

    def public_headers(self) -> tuple[PublicHeader, ...]:
        from unbake.compilers.families.ido.stdarg import header

        return (header(),)

    def public_defines(self) -> tuple[str, ...]:
        from unbake.compilers.families.ido.stdarg import SELECTOR

        return ("-D" + SELECTOR + "=1",)

    def region_name(self) -> str:
        return "ido"

    def recognizes_idioms(self, counts: Mapping[str, int], minimum: int, numerator: int, denominator: int) -> bool:
        total = sum(counts.values())
        return total >= minimum and counts.get(self.move_idiom(), 0) * denominator >= total * numerator

    def probe_commands(
        self, cache: Path, spec: CompilerSpec, policy: Host, project: PendingProject, work: Path, source: Path
    ) -> tuple[list[str], ...]:
        return ([str(cache / spec.cc), *spec.cflags, "-c", str(source), "-o", str(work / "probe.o")],)

    def assembler_release(self) -> str | None:
        return None

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
        dependency = render(("{cc}", "{preprocess}", "-M", "{source}"))
        return (
            f"{dependency} > $(@D)/$(*F).deps && sed 's|^[^:]*:|$(@D)/$(*F).i:|' $(@D)/$(*F).deps > $(@D)/$(*F).d"
            f" && rm $(@D)/$(*F).deps && {render(preprocess)} > $(@D)/$(*F).i"
        )

    def runtime_helpers(
        self, data: bytes, read_memory: Callable[[int, int], bytes]
    ) -> tuple[tuple[int, RuntimeHelper], ...]:
        return ()

    def native_templates(self) -> dict[str, tuple[str, ...] | None]:
        return {
            "preprocess": ("{cc}", "{preprocess}", "-E", "{source}"),
            "compile": ("{cc}", "{codegen}", "-c", "{name}.i", "-o", "{name}.o"),
            "assemble": None,
        }

    def uses_host_cpp(self) -> bool:
        return False

    def preserve_padding(self) -> bool:
        return False

    def assembly_flags(self, extra: tuple[str, ...]) -> tuple[str, ...]:
        return ()

    def accepts_codegen(self, flag: str) -> bool:
        import re

        return (
            flag in ("-ansi", "-fsigned-char")
            or re.fullmatch(r"-G[0-9]+|-mips[1-4]|-O[0-3s]?|-g[0-3]?", flag) is not None
        )

    def analysis_cppflags(self, flags: tuple[str, ...]) -> tuple[str, ...]:
        return ()

    def analysis_location_flags(self) -> tuple[str, ...]:
        from unbake.compilers.families.gcc import Gcc

        return Gcc().analysis_location_flags()

    def token_view(self, output: str, source: str, filename: str, boundary_line: int) -> View:
        from unbake.compilers.families.gcc.token_locations import decode, source_output

        return decode(source_output(output, boundary_line), source, filename)

    def m2c_registers(self, assembly: str, flags: tuple[str, ...], function: str) -> str:
        return assembly

    def m2c_section(self) -> str:
        # Input to the external decompiler, independent of emitted object selectors.
        return ".rodata"

    def schedule_available(self) -> bool:
        return False

    def collect_allocation(self, project: Project, policy: Host, source: Path, version: str, work: Path) -> Allocation:
        from unbake.compilers import drivers
        from unbake.config import Held
        from unbake.decomp.explain import _absolute_includes
        from unbake.process import run_tool

        flags = _absolute_includes(project, drivers.flags(project, version, source.stem))
        run_tool(
            [
                str(project.compiler_for(source).cc),
                *flags,
                *self.dump_flags(),
                str(source),
                "-o",
                str(work / "source.s"),
            ],
            work,
            "explain",
        )
        listing = work / "source.s"
        if not listing.is_file():
            raise Held(
                cause_named(
                    "dumps.ido",
                    "dumps.ido: -K emitted no textual assignment listing",
                    owner="compilers.families.ido.__init__",
                    stage="explain",
                )
            )
        return self.allocation({"ido": listing.read_text()})

    def collect_schedule(self, project: Project, policy: Host, source: Path, version: str, work: Path) -> Schedule:
        return self.schedule(None)

    def allocation_hints(self, difference: RegisterDifference, by_number: dict[int, Pseudo]) -> list[str]:
        return []

    def dependency_paths(self, output: str) -> tuple[str, ...]:
        """IDO emits one complete, unescaped filename per target rule."""
        from unbake.compilers.families import dependency_rules

        return dependency_rules(output)

    def analysis_flags(
        self,
        compiler: Path,
        cpp: str,
        root: Path,
        preprocess: tuple[str, ...],
        codegen: tuple[str, ...],
        temporary_root: Path,
    ) -> tuple[str, ...]:
        """Use the pinned native driver's implicit C environment for host token analysis.

        IDO has no macro-dump switch. Its -show output exposes the actual cfe
        definitions/search roots, including definitions applied after user flags.
        Keep native ordering; remove the host provider's unrelated predefined names.
        """
        import re
        import shlex
        import tempfile

        from unbake import atomic, inputs, process
        from unbake.cache import memo
        from unbake.config import Held

        native = self.preprocess_flags((), codegen)

        def observed() -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
            with tempfile.TemporaryDirectory(prefix="ido-analysis-", dir=temporary_root) as temporary:
                source = Path(temporary) / "empty.c"
                atomic.fresh(source, b"")
                result = process.run_native(
                    [str(compiler), *native, "-E", "-show", str(source)],
                    root,
                    "compile",
                    temporary_root=Path(temporary),
                )
                lines = [line for line in result.stderr.splitlines() if line.startswith("/usr/lib/cfe ")]
                if len(lines) != 1 or str(source) not in lines[0]:
                    raise Held(
                        Fault(
                            cause_named(
                                "compile.analysis_environment",
                                "compile.analysis_environment: IDO did not report one cfe invocation",
                                owner="compilers.families.ido.__init__",
                                stage="compile",
                            ),
                            (result,),
                        )
                    )
                before, _, after = lines[0].partition(str(source))
                before_words, after_words = shlex.split(before), shlex.split(after)
                defines = tuple(word for word in before_words if word.startswith(("-D", "-U")))
                final = tuple(word for word in after_words if word.startswith(("-D", "-U")))
                includes = tuple(word for word in before_words if word.startswith("-I") and len(word) > 2)
                macros = process.run_tool(
                    [cpp, "-undef", "-nostdinc", "-dM", "-x", "c", str(source)],
                    root,
                    "compile",
                    temporary_root=Path(temporary),
                )
                removed = tuple("-U" + name for name in re.findall(r"^#define\s+(\w+)", macros, re.M))
                return defines, final, includes, removed

        defines, final, includes, removed = memo(
            "ido.analysis-environment",
            (compiler, inputs.signature(compiler), cpp, inputs.signature(Path(cpp)), native),
            observed,
            size=retention.memory_size,
            copy_out=retention.clone,
        )
        # Both pinned cfe versions evaluate high-bit character constants unsigned.
        return (
            "-undef",
            "-nostdinc",
            "-funsigned-char",
            "-D__UNBAKE_HEADER_ANALYSIS=1",
            *removed,
            *defines,
            *preprocess,
            *includes,
            *final,
        )

    def preprocess_flags(self, preprocess: tuple[str, ...], codegen: tuple[str, ...]) -> tuple[str, ...]:
        """Language/macro input, excluding code generation optimization."""
        for flag in codegen:
            if not (
                flag.startswith(("-O", "-G", "-g", "-mips"))
                or flag in ("-non_shared", "-shared", "-ansi", "-signed", "-unsigned", "-Xc", "-Xansi")
            ):
                from unbake.config import Held

                raise Held(
                    cause_named(
                        "compile.flags",
                        f"compile.flags: {flag}: unsupported by the ido driver",
                        owner="compilers.families.ido.__init__",
                        stage="compile",
                    )
                )
        return (*preprocess, *self.public_defines(), *(flag for flag in codegen if not flag.startswith(("-O", "-g"))))

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


def adapter() -> Ido:
    return Ido()
