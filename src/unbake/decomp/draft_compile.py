"""Check a draft with its real unit compiler, recipe and include graph."""

from pathlib import Path

from unbake.decomp.explain import _absolute_includes
from unbake.decomp.trial_compile import run_tool
from unbake.project import makefile
from unbake.project.config import Held, Policy, Project
from unbake.project_tools import atomic as atomic_files
from unbake.project_tools.sn64_cc import partition_flags


def prove(project: Project, policy: Policy, function: str, version: str, source: Path) -> None:
    """Generate code without publishing a trial or touching the object cache."""
    unit = project.src / (function + ".c")
    compiler = project.compiler_for(unit)
    flags = [*_absolute_includes(project, makefile.flags(project, version, unit)), "-DNON_MATCHING=1"]
    if compiler.kind == "sn64":
        cppflags, codeflags = partition_flags(flags)
        recipe = makefile.recipe(project)
        cpp = makefile.host_executable(policy, recipe.cpp or "policy:cpp", "cpp")
        expanded = run_tool([cpp, *recipe.cppflags, *cppflags, str(source)], project.root, "m2c")
        preprocessed = source.with_suffix(".i")
        atomic_files.text(preprocessed, expanded)
        run_tool(
            [str(compiler.cc), "-quiet", *codeflags, str(preprocessed), "-o", str(source.with_suffix(".s"))],
            project.root,
            "m2c",
        )
    elif compiler.kind == "ido":
        run_tool(
            [str(compiler.cc), *flags, "-c", str(source), "-o", str(source.with_suffix(".o"))], project.root, "m2c"
        )
    else:
        raise Held("m2c", f"{function}: cannot prove draft with compiler kind {compiler.kind}")
