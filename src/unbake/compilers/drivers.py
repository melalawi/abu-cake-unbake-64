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

from unbake.config import Held, Project

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
        "preprocess": ("{cc}", "{includes}", "{codegen}", "{defines}", "-E", "{source}"),
        "compile": ("{cc}", "{codegen}", "-c", "{name}.i", "-o", "{name}.o"),
        "assemble": None,
    },
    "sn64": {
        "preprocess": ("{cpp}", "{includes}", "{cppflags}", "{defines}", "{source}"),
        "compile": ("{cc}", "-quiet", "{codegen}", "{name}.i", "-o", "{name}.s"),
        "assemble": ("{n64link}", "asn64", "--as", "{as}", "{asflags}", "{name}.s", "-o", "{name}.o"),
    },
}
DEPEND = ("{cpp}", "-MM", "-MG", "{includes}", "{defines}", "{source}")
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
    result = [
        f"-I{include.relative_to(root) if include.is_relative_to(root) else include}" for include in project.include
    ]
    for flag in project.compiler_for(unit).cflags:
        if flag not in result:
            result.append(flag)
    result.extend("-D" + macro for macro in project.version(version).macros)
    if non_matching:
        result.append("-DNON_MATCHING=1")
    guard = consumer_define(project, unit)
    if guard is not None:
        result.append(guard)
    result.extend(project.unit_flags.get(unit, ()))
    return result


def codegen_flags(values: list[str]) -> list[str]:
    """Remove preprocessing options: a .i input is already preprocessed."""
    result = []
    skip = False
    for flag in values:
        if skip:
            skip = False
        elif flag in PREPROCESSOR_PAIRS:
            skip = True
        elif not flag.startswith(("-I", "-D", "-U")) and flag != "-c":
            result.append(flag)
    if skip:
        raise Held("compile", "compile.flags: a preprocessor option is missing its value")
    return result


def partition_sn64(values: list[str]) -> tuple[list[str], list[str]]:
    """(preprocessor flags, cc1 flags) for the sn64 kind; any other flag is refused by name."""
    preprocess, compile_ = [], []
    previous = False
    for flag in values:
        if previous:
            preprocess.append(flag)
            previous = False
        elif flag in {"-I", "-D", "-U", "-include"}:
            preprocess.append(flag)
            previous = True
        elif flag.startswith(("-I", "-D", "-U")):
            preprocess.append(flag)
        elif flag.startswith(("-G", "-m", "-f", "-O", "-g", "-d")):
            compile_.append(flag)
        elif flag != "-c":
            raise Held("compile", f"compile.flags: {flag}: unsupported by the sn64 driver")
    if previous:
        raise Held("compile", "compile.flags: a preprocessor option is missing its value")
    return preprocess, compile_


def gnu_as_flags(project: Project) -> tuple[str, ...]:
    """The proven replacement for ASN64's -mips3 recipe."""
    return (*GNU_AS_FLAGS, *(flag for flag in project.sn64_asflags if flag != "-mips3"))


def _split(values: list[str]) -> tuple[list[str], list[str], list[str]]:
    """(includes, codegen, defines) in their original relative order."""
    includes: list[str] = []
    codegen: list[str] = []
    defines: list[str] = []
    pending = iter(values)
    for flag in pending:
        if flag in ("-I", "-D", "-U", "-include", "-isystem", "-iquote", "-imacros"):
            value = next(pending, None)
            if value is None:
                raise Held("compile", f"compile.flags: {flag}: missing value")
            (includes if flag not in ("-D", "-U") else defines).extend((flag, value))
        elif flag.startswith("-I"):
            includes.append(flag)
        elif flag.startswith(("-D", "-U")):
            defines.append(flag)
        elif flag != "-c":
            codegen.append(flag)
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
    project_includes = [f"-I{p.relative_to(root) if p.is_relative_to(root) else p}" for p in project.include]
    c_includes, c_codegen, c_defines = compiler_parts(project, compiler.id)
    u_includes, u_codegen, u_defines = unit_parts(project, unit)
    defines = [*c_defines, *("-D" + macro for macro in project.version(version).macros)]
    if non_matching:
        defines.append("-DNON_MATCHING=1")
    guard = consumer_define(project, unit)
    if guard is not None:
        defines.append(guard)
    defines.extend(u_defines)
    codegen = [*c_codegen, *u_codegen]
    if compiler.kind == "sn64":
        partition_sn64(codegen)
    cc = str(compiler.cc.relative_to(root) if compiler.cc.is_relative_to(root) else compiler.cc)
    return Parts(compiler.kind, cc, (*project_includes, *c_includes, *u_includes), tuple(codegen), tuple(defines))


def steps(project: Project, version: str, unit: str, source: str, tools: Tools, *, non_matching: bool = False) -> Steps:
    """The commands for UNIT from SOURCE (a path relative to the project root, or any quoted word)."""
    unit_parts_ = parts(project, version, unit, non_matching=non_matching)
    values = {
        "cc": (unit_parts_.cc,),
        "cpp": (tools.cpp,),
        "as": (tools.mips_as,),
        "n64link": (tools.n64link,),
        "includes": unit_parts_.includes,
        "codegen": unit_parts_.codegen,
        "defines": unit_parts_.defines,
        "cppflags": project.cppflags,
        "asflags": gnu_as_flags(project),
        "source": (source,),
        "name": (Path(unit).name,),
    }
    template = TEMPLATES[unit_parts_.kind]
    assemble = template["assemble"]
    return Steps(
        unit_parts_.kind,
        render(template["preprocess"] or (), values),
        render(template["compile"] or (), values),
        render(assemble, values) if assemble is not None else None,
    )


def preprocessor_options(project: Project, version: str, unit: str, *, absolute: bool) -> list[str]:
    """The unit's -I/-D/-U/-include/-isystem options; absolute include paths for callers outside the root."""
    result: list[str] = []
    pending = iter(flags(project, version, unit))
    for flag in pending:
        if flag in ("-I", "-D", "-U", "-include", "-isystem"):
            value = next(pending, None)
            if value is None:
                raise Held("compile", f"compile.flags: {flag}: missing value")
            if absolute and flag in ("-I", "-include", "-isystem") and not Path(value).is_absolute():
                value = str(project.root / value)
            result.extend((flag, value))
        elif flag.startswith(("-I", "-D", "-U")):
            if absolute and flag.startswith("-I") and not Path(flag[2:]).is_absolute():
                flag = "-I" + str(project.root / flag[2:])
            result.append(flag)
    return result


def preprocess_command(
    project: Project, cpp: str, version: str, unit: str, source: Path, *, line_markers: bool = False
) -> list[str]:
    """Preprocess SOURCE as the build would for UNIT (with NON_MATCHING defined), runnable from any directory."""
    compiler = project.compiler_for(unit)
    options = preprocessor_options(project, version, unit, absolute=True)
    if compiler.kind == "sn64":
        cppflags = [flag for flag in project.cppflags if not (line_markers and flag == "-P")]
        return [cpp, *cppflags, *options, "-DNON_MATCHING=1", str(source)]
    codegen = [
        flag for flag in flags(project, version, unit) if flag != "-c" and not flag.startswith(("-I", "-D", "-U"))
    ]
    return [str(compiler.cc), *codegen, *options, "-DNON_MATCHING=1", "-E", str(source)]
