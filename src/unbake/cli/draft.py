"""draft: write a first C draft of one function from its assembly."""

from __future__ import annotations

import argparse
import re

from unbake.cli.args import Context
from unbake.cli.output import Result
from unbake.config import Held

NAME = "draft"
HELP = "Write a first C draft of FUNC to build/work/FUNC/FUNC.c."
DESCRIPTION = """\
Write a first C draft of one function from its assembly, using the solved types.
The draft goes to build/work/FUNC/FUNC.c. Edit it there, then compare it.

  unbake draft func_80012345
  unbake draft func_80012345 --replace     # overwrite an existing draft
"""
PROJECT = "ready"


def READ_ONLY(args: argparse.Namespace) -> bool:  # noqa: N802 - verb protocol
    return True


def register(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("function", metavar="FUNC", help="Function name, as `unbake next` prints it.")
    parser.add_argument("--replace", action="store_true", help="Replace an existing draft.")


def run(context: Context) -> Result:
    from unbake.work import draft

    function = context.args.function
    if not re.fullmatch(r"[A-Za-z_]\w*", function):
        raise Held("draft", f"draft.function: {function}: expected a C identifier")
    project, host = context.ready("extract", "types", "buildfiles")
    made = draft.draft(project, host, function, replace=context.args.replace)
    data = {"function": function, "file": str(made.file), "versions": list(made.versions)}
    lines = [f"draft: {made.file}", f"versions: {', '.join(made.versions)}"]
    return Result.ok(NAME, data, lines, context.cmd("compare", made.file))
