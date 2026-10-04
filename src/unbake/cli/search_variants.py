"""search-variants: search source variants for a closer match."""

from __future__ import annotations

import argparse
from pathlib import Path

from unbake.cli.args import Context, positive
from unbake.cli.output import Result

NAME = "search-variants"
HELP = "Search source variants of FILE for a closer match."
DESCRIPTION = """\
Try many small rewrites of a draft (statement order, register pressure, the permuter) and keep the
best one. The best variant is written next to FILE as FILE.best.c.

  unbake search-variants build/work/F/F.c --method order --seconds 120
"""
PROJECT = "ready"


def READ_ONLY(args: argparse.Namespace) -> bool:  # noqa: N802 - verb protocol
    return True


def register(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("file", type=Path, metavar="FILE")
    parser.add_argument("--method", required=True, choices=("order", "registers", "permute"))
    parser.add_argument("--seconds", required=True, type=positive, metavar="N", help="Wall-clock budget.")


def run(context: Context) -> Result:
    from unbake.work import search

    project, host = context.ready("buildfiles")
    found = search.search(project, host, context.args.file.resolve(), context.args.method, context.args.seconds)
    return Result.ok(NAME, found.document(), found.lines(), context.cmd("compare", found.best_file))
