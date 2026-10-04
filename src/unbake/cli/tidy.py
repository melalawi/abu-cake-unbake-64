"""tidy: rewrite a draft to use shared types and macros."""

from __future__ import annotations

import argparse
from pathlib import Path

from unbake.cli.args import Context
from unbake.cli.output import Result

NAME = "tidy"
HELP = "Rewrite FILE to use shared types and macros."
DESCRIPTION = """\
Rewrite a draft so it uses the project's shared types, generated headers and macros instead of raw
casts and offsets. The file is rewritten in place; compare it again afterwards.

  unbake tidy build/work/func_80012345/func_80012345.c
"""
PROJECT = "ready"


def READ_ONLY(args: argparse.Namespace) -> bool:  # noqa: N802 - verb protocol
    return True


def register(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("file", type=Path, metavar="FILE")


def run(context: Context) -> Result:
    from unbake.work import tidy

    lines = tidy.tidy(context.project(), context.require_host(), context.args.file.resolve())
    return Result.ok(NAME, {"file": str(context.args.file)}, lines, context.cmd("compare", context.args.file))
