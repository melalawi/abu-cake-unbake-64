"""The exact commands that compile one C unit for one version: one source of argv for make and the runner.

Two invocation kinds exist in the registry:
- ido:  the compiler's own driver preprocesses (`cc -E`), then compiles the .i (`cc ... -c UNIT.i`).
- sn64: host cpp preprocesses, cc1 compiles to assembly, `n64link asn64` normalises it for GNU as,
        and GNU as assembles (KMC gcc 2.7.2 and SN64 gcc 2.8.1 both use this path).
Every path is relative to the project root, where make and the runner both run.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from unbake import atomic as atomic_files
from unbake.config import Held, Host, Project

PREPROCESSOR_PAIRS = frozenset({"-I", "-D", "-U", "-include", "-imacros", "-isystem", "-iquote"})
GNU_AS_FLAGS = ("-march=vr4300", "-mabi=32", "-EB", "-G0", "--no-pad-sections")


@dataclass(frozen=True)
class Tools:
    """How each host tool is spelled: absolute paths for the runner, $(VARS) for the Makefile."""

    cpp: str
    mips_as: str
    n64link: str


MAKE_TOOLS = Tools("$(CPP)", "$(AS)", "$(N64LINK)")

# One template per invocation kind. Each {field} is a list of words (runner) or one make variable (Makefile).
# preprocess writes UNIT.i on stdout from the project root; compile and assemble run in the object's directory.
TEMPLATES: dict[str, dict[str, tuple[str, ...] | None]] = {
    "ido": {
        "preprocess": ("{cc}", "{preprocess}", "-E", "{source}"),
        "compile": ("{cc}", "{codegen}", "-c", "{name}.i", "-o", "{name}.o"),
        "assemble": None,
    },
    "sn64": {
        "preprocess": ("{cpp}", "{cppflags}", "{preprocess}", "{source}"),
        "compile": ("{cc}", "-quiet", "{codegen}", "{name}.i", "-o", "{name}.s"),
        "assemble": ("{n64link}", "asn64", "--as", "{as}", "{asflags}", "{name}.s", "-o", "{name}.o"),
    },
}
DEPEND = ("{cpp}", "-MM", "-MG", "{cppflags}", "{preprocess}", "{source}")
# Kinds whose objects keep trailing zero padding after the last function (n64link place --trim otherwise).
UNTRIMMED = frozenset({"sn64"})


@dataclass(frozen=True)
class Steps:
    """One unit's commands. preprocess writes stdout to UNIT.i; compile and assemble run in the unit's directory."""

    kind: str
    preprocess: tuple[str, ...]
    compile: tuple[str, ...]
    assemble: tuple[str, ...] | None


@dataclass(frozen=True)
class Parts:
    """A unit's flags split the way every template uses them."""

    kind: str
    cc: str
    includes: tuple[str, ...]
    codegen: tuple[str, ...]
    defines: tuple[str, ...]
    preprocess: tuple[str, ...]
    effective: tuple[str, ...]


def render(template: tuple[str, ...], values: dict[str, tuple[str, ...]]) -> tuple[str, ...]:
    """Fill a template: a word that is exactly {field} splices that field's words."""
    result: list[str] = []
    for word in template:
        if word.startswith("{") and word.endswith("}"):
            result.extend(values[word[1:-1]])
        else:
            result.append(word.format_map({key: " ".join(value) for key, value in values.items()}))
    return tuple(result)


def consumer_define(project: Project, unit: str) -> str | None:
    """Units with a generated consumer header see their own guard macro."""
    if any((include / "shared" / "consumers" / f"{unit}.h").is_file() for include in project.include):
        return "-DUNBAKE_CONSUMER_" + hashlib.sha256(unit.encode()).hexdigest()[:16].upper() + "=1"
    return None


def flags(project: Project, version: str, unit: str, *, non_matching: bool = False) -> list[str]:
    """Include roots, compiler flags, version macros, the consumer guard and the unit's own flags."""
    root = project.root
    unit = Path(unit).stem
    result = [
        f"-I{include.relative_to(root) if include.is_relative_to(root) else include}" for include in project.include
    ]
    result.extend(project.compiler_for(unit).cflags)
    result.extend("-D" + macro for macro in project.version(version).macros)
    if non_matching:
        result.append("-DNON_MATCHING=1")
    guard = consumer_define(project, unit)
    if guard is not None:
        result.append(guard)
    result.extend(project.unit_flags.get(unit, ()))
    return result


def _options(values: list[str]) -> tuple[list[str], list[str]]:
    """Ordered preprocessing options and code generation options, with named pair errors."""
    preprocess: list[str] = []
    codegen: list[str] = []
    pending = iter(values)
    for flag in pending:
        if flag in PREPROCESSOR_PAIRS:
            value = next(pending, None)
            if value is None or not value or value.startswith("-"):
                raise Held("compile", f"compile.flags: {flag}: missing value")
            preprocess.extend((flag, value))
        elif flag.startswith(("-I", "-D", "-U")):
            preprocess.append(flag)
        elif flag != "-c":
            codegen.append(flag)
    return preprocess, codegen


def _supported(kind: str, values: list[str]) -> None:
    import re
    import tomllib

    from unbake import inputs
    from unbake.cache import memo
    from unbake.compilers.registry import REGISTRY_PATH

    def read() -> frozenset[str]:
        definitions = tomllib.loads(REGISTRY_PATH.read_text())["compilers"]
        return frozenset(
            flag
            for spec in definitions.values()
            if spec["kind"] == kind
            for flags in [spec["cflags"], *spec["flag_variants"]]
            for flag in flags
        ) | {"-ansi", "-fsigned-char"}

    supported = memo("compiler.supported-flags", (kind, REGISTRY_PATH, inputs.signature(REGISTRY_PATH)), read, keep=8)
    for flag in values:
        if flag in supported or re.fullmatch(r"-G[0-9]+|-mips[1-4]|-O[0-3s]?|-g[0-3]?", flag):
            continue
        raise Held("compile", f"compile.flags: {flag}: unsupported by the {kind} driver")


def codegen_flags(values: list[str]) -> list[str]:
    return _options(values)[1]


def stage_flags(kind: str, values: list[str]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """The family's explicit stage contract, shared by all native consumers."""
    from unbake.compilers.families.gcc import Gcc
    from unbake.compilers.families.ido import Ido

    preprocess, codegen = _options(values)
    _supported(kind, codegen)
    adapter: Gcc | Ido
    if kind == "sn64":
        adapter = Gcc()
    elif kind == "ido":
        adapter = Ido()
    else:
        raise Held("compile", f"compile.kind: {kind}: unsupported driver")
    return adapter.preprocess_flags(tuple(preprocess), tuple(codegen)), tuple(codegen)


def gnu_as_flags(project: Project) -> tuple[str, ...]:
    """The proven replacement for ASN64's -mips3 recipe."""
    return (*GNU_AS_FLAGS, *(flag for flag in project.sn64_asflags if flag != "-mips3"))


def _split(values: list[str]) -> tuple[list[str], list[str], list[str]]:
    """(includes, codegen, defines) in their original relative order."""
    preprocess, codegen = _options(values)
    includes: list[str] = []
    defines: list[str] = []
    pending = iter(preprocess)
    for flag in pending:
        destination = defines if flag.startswith(("-D", "-U")) else includes
        destination.append(flag)
        if flag in PREPROCESSOR_PAIRS:
            destination.append(next(pending))
    return includes, codegen, defines


def compiler_parts(project: Project, ident: str) -> tuple[list[str], list[str], list[str]]:
    """A configured compiler's own flags as (includes, codegen, defines)."""
    return _split(list(project.compilers[ident].cflags))


def unit_parts(project: Project, unit: str) -> tuple[list[str], list[str], list[str]]:
    """A unit's extra [units] flags as (includes, codegen, defines)."""
    return _split(list(project.unit_flags.get(unit, ())))


def parts(project: Project, version: str, unit: str, *, non_matching: bool = False) -> Parts:
    """Everything a template needs for UNIT in VERSION; the Makefile composes the same lists from variables."""
    compiler = project.compiler_for(unit)
    if compiler.kind not in TEMPLATES:
        raise Held("compile", f"compiler.{compiler.id}.kind: {compiler.kind}: no driver")
    root = project.root
    effective = flags(project, version, unit, non_matching=non_matching)
    preprocess, codegen = stage_flags(compiler.kind, effective)
    includes, _, defines = _split(effective)
    cc = str(compiler.cc.relative_to(root) if compiler.cc.is_relative_to(root) else compiler.cc)
    return Parts(compiler.kind, cc, tuple(includes), codegen, tuple(defines), preprocess, tuple(effective))


def steps(project: Project, version: str, unit: str, source: str, tools: Tools, *, non_matching: bool = False) -> Steps:
    """The commands for UNIT from SOURCE (a path relative to the project root, or any quoted word)."""
    owned = parts(project, version, unit, non_matching=non_matching)
    return from_flags(
        owned.kind, owned.cc, owned.effective, project.cppflags, gnu_as_flags(project), unit, source, tools
    )


def from_flags(
    kind: str,
    cc: str,
    effective: tuple[str, ...],
    cppflags: tuple[str, ...],
    asflags: tuple[str, ...],
    unit: str,
    source: str,
    tools: Tools,
) -> Steps:
    """Render the one ordered native stage contract, including overlay proofs."""
    preprocess, codegen = stage_flags(kind, list(effective))
    values = {
        "cc": (cc,),
        "cpp": (tools.cpp,),
        "as": (tools.mips_as,),
        "n64link": (tools.n64link,),
        "preprocess": preprocess,
        "codegen": codegen,
        "cppflags": cppflags,
        "asflags": asflags,
        "source": (source,),
        "name": (Path(unit).name,),
    }
    template = TEMPLATES[kind]
    assemble = template["assemble"]
    return Steps(
        kind,
        render(template["preprocess"] or (), values),
        render(template["compile"] or (), values),
        render(assemble, values) if assemble is not None else None,
    )


def preprocess_command(
    project: Project, cpp: str, version: str, unit: str, source: Path, *, non_matching: bool, line_markers: bool = False
) -> list[str]:
    """Exactly the build stage; callers run from the project root."""
    commands = steps(project, version, unit, str(source), Tools(cpp, "", ""), non_matching=non_matching)
    argv = list(commands.preprocess)
    if commands.kind == "ido":
        argv[0] = str(project.compiler_for(unit).cc)
    elif line_markers:
        argv = [flag for flag in argv if flag != "-P"]
    return argv


def analysis_command(project: Project, policy: Host, version: str, unit: str) -> list[str]:
    """GCC token-location analysis with the actual unit's ordered macro/include environment.

    The analysis provider is host cpp even for an IDO unit. Native compilation
    remains owned by that unit's family; GCC analysis switches never reach IDO.
    """
    from unbake import scratch
    from unbake.compilers.families import family_for

    cpp = str(policy.cpp)
    compiler = project.compiler_for(unit)
    preprocess, codegen = _options(flags(project, version, unit))
    options = family_for(compiler).analysis_flags(
        compiler.cc, cpp, project.root, tuple(preprocess), tuple(codegen), scratch.root(policy, project, "compile")
    )
    return [cpp, *(project.cppflags if compiler.kind == "sn64" else ()), *options, "-x", "c", "-"]


def preprocess_text(project: Project, cpp: str, version: str, unit: str, text: str, phase: str) -> str:
    """Preprocess in-memory C through the same family stage, with source ownership supplied."""
    import tempfile

    from unbake.process import run_tool

    project.build.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".preprocess-", dir=project.build) as temporary:
        source = Path(temporary) / "unit.c"
        atomic_files.fresh(source, text.encode())
        return run_tool(
            preprocess_command(project, cpp, version, unit, source, non_matching=True),
            project.root,
            phase,
            context={"function": unit, "version": version},
        )
