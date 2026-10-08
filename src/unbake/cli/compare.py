"""compare: compile one C file and compare it with the ROM in every version that holds the function."""

from __future__ import annotations

import argparse
from pathlib import Path

from unbake.cli.args import Context
from unbake.cli.output import Result

NAME = "compare"
HELP = "Compile FILE and compare it with the ROM in every holding version."
DESCRIPTION = """\
Compile one C file and compare its machine code with the original, in every version that holds the
function. Prints the match percent per version. When every version is exact, the Next line is publish.

  unbake compare build/work/func_80012345/func_80012345.c

--require-version VERSION (repeatable) chooses compilers for that required scope,
including every already published C version. Every holding version is still measured;
optional version faults remain explicit. Global exact remains an all-version claim.

--explain-schedule reruns only the chosen recipe for versions with register/order
differences and attaches bounded compiler decisions to those regions. Missing
fields and ambiguous mappings are explicit; normal compare collects no dumps.
"""
PROJECT = "ready"


def READ_ONLY(args: argparse.Namespace) -> bool:
    return True


def register(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--source-root",
        action="append",
        type=Path,
        default=[],
        help="Explicitly admit an external source/include root.",
    )
    parser.add_argument(
        "--include-root",
        action="append",
        type=Path,
        default=[],
        help="Required include search root for this source scope.",
    )
    parser.add_argument("--flags", action="store_true", help="Run one finite evidence-conditioned option episode.")
    parser.add_argument("--recipe", type=Path, help="Serialized phase recipe for the admitted TU.")
    parser.add_argument("file", type=Path, metavar="FILE", help="build/work/FUNC/FUNC.c, FUNC.best.c or src/FUNC.c")
    parser.add_argument(
        "--explain-schedule",
        action="store_true",
        help="Collect compiler scheduler/allocator facts for register/order regions.",
    )
    parser.add_argument(
        "--require-version",
        action="append",
        metavar="VERSION",
        help="Require this version and already published C versions; repeatable.",
    )


def run(context: Context) -> Result:
    from dataclasses import replace

    from unbake import steps, strict_json
    from unbake.compilers.recipe_options import UnitRecipe
    from unbake.work import compare
    from unbake.work.source_scope import scoped_project

    project = scoped_project(
        context.project(),
        context.args.file.resolve(),
        tuple(context.args.source_root),
        tuple(context.args.include_root),
    )
    if context.args.recipe is not None:
        recipe = UnitRecipe.read(strict_json.read(context.args.recipe))
        key = project.unit_path(compare.function_of(context.args.file))
        from unbake.compilers.options import admit_trial

        admit_trial(project, key, recipe)
        project = replace(project, units={**project.units, key: recipe})
    host = context.require_host()
    steps.ensure(project, host, ("buildfiles",))
    required = tuple(context.args.require_version) if context.args.require_version is not None else None
    measured = compare.compare(
        project,
        host,
        context.args.file.resolve(),
        required_versions=required,
        explain_schedule=context.args.explain_schedule,
        flags=context.args.flags,
    )
    data = measured.document()
    if measured.unit_recipe is not None:
        import json

        from unbake import atomic

        atomic.text(
            context.args.file.with_suffix(".recipe.json"), json.dumps(measured.unit_recipe, sort_keys=True) + "\n"
        )
    from unbake.work.source_scope import admit_source

    measured.next_action = admit_source(project, measured.file).saved_action(
        measured.file,
        exact=measured.required_exact,
        config=context.config_path,
        required_versions=measured.required_versions or (),
    )
    following = measured.next_command
    return Result.ok(NAME, data, measured.lines(), following)
