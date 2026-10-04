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
"""
PROJECT = "ready"


def READ_ONLY(args: argparse.Namespace) -> bool:  # noqa: N802 - verb protocol
    return True


def register(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("file", type=Path, metavar="FILE", help="build/work/FUNC/FUNC.c or src/FUNC.c")


def run(context: Context) -> Result:
    from unbake.work import compare

    project, host = context.ready("buildfiles")
    measured = compare.compare(project, host, context.args.file.resolve())
    data = measured.document()
    following = (
        context.cmd("publish", context.args.file) if measured.exact else f"stop: edit {context.args.file}, then compare again"
    )
    return Result.ok(NAME, data, measured.lines(), following)
