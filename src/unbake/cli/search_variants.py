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

--method scheduler-birth explicitly tries the mapped GCC integer birth-priority
experiment. Safety gates can produce no variant; whole-function bytes decide.
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
    parser.add_argument("file", type=Path, metavar="FILE")
    parser.add_argument("--require-version", action="append", default=[])
    parser.add_argument(
        "--method", required=True, choices=("auto", "creative", "order", "registers", "permute", "scheduler-birth")
    )
    parser.add_argument("--seconds", required=True, type=positive, metavar="N", help="Wall-clock budget.")


def run(context: Context) -> Result:
    from unbake import steps
    from unbake.work import search
    from unbake.work.source_scope import scoped_project

    project = scoped_project(
        context.project(),
        context.args.file.resolve(),
        tuple(context.args.source_root),
        tuple(context.args.include_root),
    )
    host = context.require_host()
    steps.ensure(project, host, ("buildfiles",))
    found = search.search(
        project,
        host,
        context.args.file.resolve(),
        context.args.method,
        context.args.seconds,
        external_roots=tuple(context.args.source_root),
        required_versions=tuple(context.args.require_version),
        config_path=context.config_path,
    )
    return Result.ok(NAME, found.document(), found.lines(), found.next_command)
