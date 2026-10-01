"""Prove GBI source rewrites against every owning compiler configuration."""

import re
import tempfile
from pathlib import Path

from unbake.decomp.explain import _absolute_includes
from unbake.decomp.trial_compile import run_tool
from unbake.layout import split
from unbake.project import makefile
from unbake.project.config import Held, Policy, Project
from unbake.project_tools.elf import Object
from unbake.project_tools.sn64_cc import partition_flags


def code(
    project: Project,
    policy: Policy,
    unit: Path,
    version: str,
    source: Path,
    mode: int,
    cache: dict[tuple[str, tuple[str, ...], str], object],
) -> object:
    """Compare emitted code and pools, retaining relocation identities."""
    compiler = project.compiler_for(unit)
    flags = [
        *_absolute_includes(project, makefile.flags(project, version, unit)),
        "-DNON_MATCHING=1" if mode else "-UNON_MATCHING",
    ]
    if compiler.kind == "sn64":
        cppflags, codeflags = partition_flags(flags)
        recipe = makefile.recipe(project)
        cpp = makefile.host_executable(policy, recipe.cpp or "policy:cpp", "cpp")
        pre = source.with_suffix(".i")
        expanded = run_tool([cpp, *recipe.cppflags, *cppflags, str(source)], project.root, "gbi")
        pre.write_text(expanded)
        key = (str(compiler.cc), tuple(codeflags), re.sub(r"^[ \t]*#[ \t]*\d+[^\n]*", "", expanded, flags=re.M))
        if key in cache:
            return cache[key]
        output = source.with_suffix(".s")
        run_tool([str(compiler.cc), "-quiet", *codeflags, str(pre), "-o", str(output)], project.root, "gbi")
        # Source filenames/debug locations do not participate in machine code.
        result = tuple(
            line.strip()
            for line in output.read_text().splitlines()
            if line.strip() and not re.match(r"\s*\.(?:file|loc|stabs?|stabn|stabd)\b", line)
        )
        cache[key] = result
        return result
    if compiler.kind != "ido":
        raise Held("gbi", f"{unit.stem}: unsupported compiler {compiler.kind}")
    output = source.with_suffix(".o")
    run_tool([str(compiler.cc), *flags, "-c", str(source), "-o", str(output)], project.root, "gbi")
    obj = Object(output)
    return tuple(
        (
            name,
            obj.content(index),
            tuple((offset, kind, symbol["name"], symbol["value"]) for offset, kind, symbol in obj.relocations(index)),
        )
        for index, name in enumerate(obj.names)
        if obj.sections[index][2] & 2
    )


def preserve(project: Project, policy: Policy, unit: Path, before: str, after: str) -> None:
    versions = [
        version
        for version in project.versions
        if any(
            project.src / (row.path + ".c") == unit
            for segment in split.layout(project.version(version).split)[2]
            for row in segment.rows
            if row.kind in ("c", "asm")
        )
    ]
    if not versions:
        raise Held("gbi", f"{unit.stem}: no owning VERSION for codegen proof")
    with tempfile.TemporaryDirectory(prefix="gbi-proof-") as temporary:
        root = Path(temporary)
        original, candidate = root / "before" / unit.name, root / "after" / unit.name
        for path, text in ((original, before), (candidate, after)):
            path.parent.mkdir()
            path.write_text(text)
        cache: dict[tuple[str, tuple[str, ...], str], object] = {}
        for version in versions:
            for mode in (0, 1):
                left = code(project, policy, unit, version, original, mode, cache)
                right = code(project, policy, unit, version, candidate, mode, cache)
                if left != right:
                    raise Held("gbi", f"{unit.stem}: VERSION {version} NON_MATCHING={mode}: rewrite changes codegen")
