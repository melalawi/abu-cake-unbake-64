"""The exact commands that compile one C unit for one version: one source of argv for make and the runner.

Registry families supply each native invocation contract.
Every path is relative to the project root, where make and the runner both run.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from unbake import atomic as atomic_files
from unbake.compilers.families import Family
from unbake.compilers.recipe_options import ResolvedRecipe, partition, resolve_options
from unbake.config import Compiler, Held, Host, Project
from unbake.process import named as cause_named

PREPROCESSOR_PAIRS = frozenset({"-I", "-D", "-U", "-include", "-imacros", "-isystem", "-iquote"})


@dataclass(frozen=True)
class Tools:
    """How each host tool is spelled: absolute paths for the runner, $(VARS) for the Makefile."""

    cpp: str
    mips_as: str
    n64link: str


MAKE_TOOLS = Tools("$(CPP)", "$(AS)", "$(N64LINK)")


# One template per invocation kind. Each {field} is a list of words (runner) or one make variable (Makefile).
# preprocess writes UNIT.i on stdout from the project root; compile and assemble run in the object's directory.
def templates(kind: str) -> dict[str, tuple[str, ...] | None]:
    from unbake.compilers.families import family_for_kind

    return family_for_kind(kind).native_templates()


def preserves_padding(kind: str) -> bool:
    from unbake.compilers.families import family_for_kind

    return family_for_kind(kind).preserve_padding()


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


def consumer_define(project: Project, unit: str | Path) -> str | None:
    """Units with a generated consumer header see their own guard macro."""
    if any((include / "shared" / "consumers" / f"{unit}.h").is_file() for include in project.include):
        return "-DUNBAKE_CONSUMER_" + hashlib.sha256(str(unit).encode()).hexdigest()[:16].upper() + "=1"
    return None


def resolved(project: Project, version: str, unit: str | Path) -> ResolvedRecipe:
    from unbake.compilers.registry import specification

    compiler = project.compiler_for(unit)
    spec = specification(compiler.id)
    adapter = _selection(compiler.id)[1]
    defaults = partition(compiler.cflags)
    defaults["assemble"] = list(assembly_flags(project, compiler.id))
    recipe = project.recipe_for(unit)
    binding = None
    separate = False
    if recipe.functions:
        from unbake.layout import split

        rows = [
            row
            for v in project.versions
            for row in split.functions(project, v)
            if f"src/{row.path}.c" == project.unit_path(unit)
        ]
        bindings = {split.recipe_binding(row): row for row in rows}
        if set(dict(recipe.functions)) - bindings.keys():
            raise Held(
                cause_named(
                    "recipe.scope",
                    "function binding differs from current placement",
                    owner="compilers.drivers",
                    stage="compile",
                )
            )
        if any(len(split.unit_members(bindings[key])) != 1 for key in dict(recipe.functions)):
            raise Held(
                cause_named(
                    "recipe.scope",
                    "whole-file options require a separately compiled function TU",
                    owner="compilers.drivers",
                    stage="compile",
                )
            )
        selected = [row for row in rows if row.version == version]
        if len(selected) == 1:
            binding, separate = split.recipe_binding(selected[0]), True
    try:
        result = resolve_options(
            project.unit_path(unit),
            recipe,
            defaults,
            version_macros=project.version(version).macros,
            pins=spec.pins,
            binding=binding,
            separately_compiled=separate,
            specs=adapter.option_space(spec),
        )
        _selection(compiler.id)[1].validate_options(result, compiler.cflags)
        return result
    except ValueError as error:
        raise Held(cause_named("recipe.options", str(error), owner="compilers.drivers", stage="compile")) from error


def flags(project: Project, version: str, unit: str | Path, *, non_matching: bool = False) -> list[str]:
    recipe = resolved(project, version, unit)
    result = [
        f"-I{include.relative_to(project.root) if include.is_relative_to(project.root) else include}"
        for include in project.include
    ]
    result.extend(recipe.phase("preprocess"))
    result.extend(recipe.phase("compile"))
    if non_matching:
        result.append("-DNON_MATCHING=1")
    guard = consumer_define(project, Path(project.unit_path(unit)).stem)
    if guard is not None:
        result.append(guard)
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
                raise Held(
                    cause_named(
                        "compile.flags",
                        f"compile.flags: {flag}: missing value",
                        owner="compilers.drivers",
                        stage="compile",
                    )
                )
            preprocess.extend((flag, value))
        elif flag.startswith(("-I", "-D", "-U")):
            preprocess.append(flag)
        elif flag != "-c":
            codegen.append(flag)
    return preprocess, codegen


def _supported(selector: str, values: list[str]) -> None:
    _kind, family = _selection(selector)
    from unbake.compilers.registry import registry

    specs = registry()
    baseline = (
        (*specs[selector].cflags, *specs[selector].supported_options)
        if selector in specs
        else tuple(
            flag for spec in specs.values() if spec.kind == _kind for flag in (*spec.cflags, *spec.supported_options)
        )
    )
    for flag in values:
        if flag in baseline or family.accepts_codegen(flag):
            continue
        raise Held(
            cause_named(
                "compile.flags",
                f"compile.flags: {flag}: unsupported by {selector} driver",
                owner="compilers.drivers",
                stage="compile",
            )
        )


def codegen_flags(values: list[str]) -> list[str]:
    return _options(values)[1]


def _selection(selector: str) -> tuple[str, Family]:
    from unbake.compilers.families import family_for, family_for_kind
    from unbake.compilers.registry import registry

    spec = registry().get(selector)
    return (spec.kind, family_for(selector)) if spec is not None else (selector, family_for_kind(selector))


def stage_flags(selector: str, values: list[str]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Compiler identity selects preprocessing semantics; kind selects only execution."""
    _kind, adapter = _selection(selector)
    preprocess, codegen = _options(values)
    _supported(selector, codegen)
    return adapter.preprocess_flags(tuple(preprocess), tuple(codegen)), tuple(codegen)


def assembly_flags(project: Project, compiler: str | None = None) -> tuple[str, ...]:
    """The selected unit's family owns its external assembler options."""
    from unbake.compilers.families import family_for

    return family_for(compiler or project.default_compiler).assembly_flags(project.gnu_asflags)


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


def unit_parts(project: Project, unit: str | Path) -> tuple[list[str], list[str], list[str]]:
    """A unit's extra [units] flags as (includes, codegen, defines)."""
    recipe = project.recipe_for(unit)
    return _split([*recipe.phase("preprocess"), *recipe.phase("compile")])


def parts(project: Project, version: str, unit: str | Path, *, non_matching: bool = False) -> Parts:
    """Everything a template needs for UNIT in VERSION; the Makefile composes the same lists from variables."""
    compiler = project.compiler_for(unit)
    templates(compiler.kind)
    root = project.root
    effective = flags(project, version, unit, non_matching=non_matching)
    preprocess, codegen = stage_flags(compiler.id, effective)
    includes, _, defines = _split(effective)
    cc = str(compiler.cc.relative_to(root) if compiler.cc.is_relative_to(root) else compiler.cc)
    return Parts(compiler.kind, cc, tuple(includes), codegen, tuple(defines), preprocess, tuple(effective))


def steps(
    project: Project, version: str, unit: str | Path, source: str, tools: Tools, *, non_matching: bool = False
) -> Steps:
    """The commands for UNIT from SOURCE (a path relative to the project root, or any quoted word)."""
    owned = parts(project, version, unit, non_matching=non_matching)
    recipe = resolved(project, version, unit)
    return from_flags(
        project.compiler_for(unit).id,
        owned.cc,
        owned.effective,
        project.cppflags,
        recipe.phase("assemble"),
        Path(project.unit_path(unit)).stem,
        source,
        tools,
    )


def from_flags(
    kind: str,
    cc: str,
    effective: tuple[str, ...],
    cppflags: tuple[str, ...],
    asflags: tuple[str, ...],
    unit: str | Path,
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
    kind, selected = _selection(kind)
    template = selected.native_templates()
    assemble = template["assemble"]
    return Steps(
        kind,
        render(template["preprocess"] or (), values),
        render(template["compile"] or (), values),
        render(assemble, values) if assemble is not None else None,
    )


def preprocess_command(
    project: Project,
    cpp: str,
    version: str,
    unit: str | Path,
    source: Path,
    *,
    non_matching: bool,
    line_markers: bool = False,
) -> list[str]:
    """Exactly the build stage; callers run from the project root."""
    commands = steps(project, version, unit, str(source), Tools(cpp, "", ""), non_matching=non_matching)
    argv = list(commands.preprocess)
    from unbake.compilers.families import family_for_kind

    if not family_for_kind(commands.kind).uses_host_cpp():
        argv[0] = str(project.compiler_for(unit).cc)
    elif line_markers:
        argv = [flag for flag in argv if flag != "-P"]
    return argv


def analysis_command(project: Project, policy: Host, version: str, unit: str | Path) -> list[str]:
    """Family-owned host analysis in the unit's effective macro/include environment."""
    from unbake import scratch
    from unbake.compilers.families import family_for

    cpp = str(policy.cpp)
    compiler = project.compiler_for(unit)
    preprocess, codegen = _options(flags(project, version, unit))
    options = family_for(compiler).analysis_flags(
        compiler.cc, cpp, project.root, tuple(preprocess), tuple(codegen), scratch.root(policy, project, "compile")
    )
    return [cpp, *family_for(compiler).analysis_cppflags(project.cppflags), *options, "-x", "c", "-"]


def preprocess_text(project: Project, cpp: str, version: str, unit: str | Path, text: str, phase: str) -> str:
    """Preprocess in-memory C through the same family stage, with source ownership supplied."""
    import tempfile

    project.build.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".preprocess-", dir=project.build) as temporary:
        source = Path(temporary) / "unit.c"
        atomic_files.fresh(source, text.encode())
        return run_preprocess(
            project,
            preprocess_command(project, cpp, version, unit, source, non_matching=True),
            phase,
            unit=unit,
            context={"function": unit, "version": version},
        )


def context_command(
    project: Project, cpp: str, compiler: Compiler, options: list[str], *, line_markers: bool
) -> list[str]:
    from unbake.compilers.families import family_for

    family = family_for(compiler)
    if not family.uses_host_cpp():
        return [str(compiler.cc), *options, "-E", "-"]
    return [
        cpp,
        *(flag for flag in family.analysis_cppflags(project.cppflags) if not line_markers or flag != "-P"),
        *options,
        "-x",
        "c",
        "-",
    ]


def assembler_release() -> str:
    from unbake.compilers.families import family_named
    from unbake.compilers.registry import registry

    values = {family_named(spec.family).assembler_release() for spec in registry().values()}
    values.discard(None)
    if len(values) != 1:
        raise Held(
            cause_named(
                "build.assembler_release",
                "build.assembler_release: expected one external assembler release",
                owner="compilers.drivers",
                stage="buildfiles",
            )
        )
    return next(value for value in values if value is not None)


def run_preprocess(
    project: Project,
    argv: list[str],
    phase: str,
    *,
    unit: str | Path | None = None,
    context: dict[str, object] | None = None,
    temporary_root: Path | None = None,
) -> str:
    """The actual native result must satisfy the selected family's directive contract."""
    from unbake import process
    from unbake.compilers.families import family_for

    compiler = project.compilers[project.default_compiler] if unit is None else project.compiler_for(unit)
    result = process.run_native(argv, project.root, phase, context=context, temporary_root=temporary_root)
    try:
        return family_for(compiler).preprocessed(result)
    except ValueError as error:
        raise Held(
            process.Fault(
                cause_named("preprocess.semantic", str(error), owner="compilers.drivers", stage="preprocess"), (result,)
            )
        ) from error
