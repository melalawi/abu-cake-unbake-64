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
    from unbake.work import compare

    project, host = context.ready("buildfiles")
    required = tuple(context.args.require_version) if context.args.require_version is not None else None
    measured = compare.compare(
        project,
        host,
        context.args.file.resolve(),
        required_versions=required,
        explain_schedule=context.args.explain_schedule,
    )
    data = measured.document()
    following = (
        context.cmd("publish", context.args.file, *(word for v in required or () for word in ("--require-version", v)))
        if measured.required_exact
        else f"stop: edit {context.args.file}, then compare again"
    )
    return Result.ok(NAME, data, measured.lines(), following)
