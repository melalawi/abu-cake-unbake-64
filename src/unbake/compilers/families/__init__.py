"""Compiler family behavior, selected by the compiler registry."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from unbake.compilers.families.types import Schedule

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
    from unbake.objects.elf import Object
    from unbake.objects.rodata import Pool

from unbake.config import Held
from unbake.process import capture
from unbake.process import named as cause_named


class CompilerIdentity(Protocol):
    @property
    def id(self) -> str: ...


@runtime_checkable
class Family(Protocol):
    def diagnose(self, result: Any, context: Any) -> Any | None: ...
    def source_intrinsics(self) -> tuple[str, ...]: ...
    def public_headers(self) -> tuple[PublicHeader, ...]: ...
    def public_defines(self) -> tuple[str, ...]: ...
    def region_name(self) -> str: ...
    def recognizes_idioms(self, counts: Mapping[str, int], minimum: int, numerator: int, denominator: int) -> bool: ...
    def probe_commands(
        self, cache: Path, spec: CompilerSpec, policy: Host, project: PendingProject, work: Path, source: Path
    ) -> tuple[list[str], ...]: ...
    def assembler_release(self) -> str | None: ...
    def dependency_command(self, command: list[str]) -> list[str]: ...
    def macro_command(self, command: list[str], probe: str) -> list[str]: ...
    def make_preprocess(self, render: Callable[[tuple[str, ...]], str]) -> str: ...
    def runtime_helpers(
        self, data: bytes, read_memory: Callable[[int, int], bytes]
    ) -> tuple[tuple[int, RuntimeHelper], ...]: ...
    def native_templates(self) -> dict[str, tuple[str, ...] | None]: ...
    def uses_host_cpp(self) -> bool: ...
    def preserve_padding(self) -> bool: ...
    def assembly_flags(self, extra: tuple[str, ...]) -> tuple[str, ...]: ...
    def accepts_codegen(self, flag: str) -> bool: ...
    def analysis_cppflags(self, flags: tuple[str, ...]) -> tuple[str, ...]: ...
    def analysis_location_flags(self) -> tuple[str, ...]: ...
    def token_view(self, output: str, source: str, filename: str, boundary_line: int) -> View: ...
    def m2c_registers(self, assembly: str, flags: tuple[str, ...], function: str) -> str: ...
    def m2c_section(self) -> str: ...
    def schedule_available(self) -> bool: ...
    def collect_allocation(
        self, project: Project, policy: Host, source: Path, version: str, work: Path
    ) -> Allocation: ...
    def collect_schedule(self, project: Project, policy: Host, source: Path, version: str, work: Path) -> Schedule: ...
    def allocation_hints(self, difference: RegisterDifference, by_number: dict[int, Pseudo]) -> list[str]: ...
    def dependency_paths(self, output: str) -> tuple[str, ...]: ...
    def preprocess_flags(self, preprocess: tuple[str, ...], codegen: tuple[str, ...]) -> tuple[str, ...]: ...
    def analysis_flags(
        self,
        compiler: Path,
        cpp: str,
        root: Path,
        preprocess: tuple[str, ...],
        codegen: tuple[str, ...],
        temporary_root: Path,
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
            raise Held(
                cause_named(
                    "compile.dependencies",
                    "compile.dependencies: invalid native dependency rule",
                    owner="compilers.families.__init__",
                    stage="compile",
                )
            )
        if names.strip():
            result.append(names.strip())
    return tuple(result)


def family_named(name: str) -> Family:
    """Load a family's declared adapter; no caller or central registration changes."""
    import importlib
    import re

    if not re.fullmatch(r"[a-z][a-z0-9_]*", name):
        raise Held(
            cause_named(
                "compilers.families.__init__.family_named",
                f"compiler.family {name}: unsafe module name",
                owner="compilers.families.__init__",
                stage="families",
            )
        )
    try:
        module = importlib.import_module(f"{__name__}.{name}")
        adapter = module.adapter()
    except (ImportError, AttributeError) as error:
        raise Held(
            capture(
                error,
                cause=cause_named(
                    "compilers.families.__init__.family_named",
                    f"compiler.family {name}: missing adapter",
                    owner="compilers.families.__init__",
                    stage="families",
                ),
            )
        ) from error
    if not isinstance(adapter, Family):
        raise Held(
            cause_named(
                "compilers.families.__init__.family_named",
                f"compiler.family {name}: incomplete protocol",
                owner="compilers.families.__init__",
                stage="families",
            )
        )
    return adapter


def family_for(compiler: str | CompilerIdentity) -> Family:
    from unbake.compilers.registry import specification

    ident = compiler if isinstance(compiler, str) else compiler.id
    if not ident:
        raise Held(
            cause_named(
                "compiler.id", "compiler.id: missing value", owner="compilers.families.__init__", stage="families"
            )
        )
    return family_named(specification(ident).family)


def family_for_kind(kind: str) -> Family:
    from unbake.compilers.registry import registry

    names = {spec.family for spec in registry().values() if spec.kind == kind}
    if not names:
        raise Held(
            cause_named(
                "compile.kind",
                f"compile.kind: {kind}: missing family contract",
                owner="compilers.families.__init__",
                stage="compile",
            )
        )
    adapters = [family_named(name) for name in sorted(names)]
    first = adapters[0]
    if any(
        adapter.native_templates() != first.native_templates()
        or adapter.preserve_padding() != first.preserve_padding()
        or adapter.uses_host_cpp() != first.uses_host_cpp()
        for adapter in adapters[1:]
    ):
        raise Held(
            cause_named(
                "compile.kind",
                f"compile.kind: {kind}: families disagree on execution path",
                owner="compilers.families.__init__",
                stage="compile",
            )
        )
    return first


def constant_sections() -> frozenset[str]:
    """All registry families' constant selectors for compiler-neutral object readers."""
    from unbake.compilers.registry import registry

    return frozenset(family_named(name).rodata_section() for name in {s.family for s in registry().values()})


def family_from_idioms(counts: Mapping[str, int], minimum: int, numerator: int = 4, denominator: int = 5) -> str | None:
    from unbake.compilers.registry import registry

    matches = [
        name
        for name in sorted({spec.family for spec in registry().values()})
        if family_named(name).recognizes_idioms(counts, minimum, numerator, denominator)
    ]
    return matches[0] if len(matches) == 1 else None
