"""publish: land exact functions: ROM proof, write, commit and push."""

from __future__ import annotations

import argparse
from pathlib import Path

from unbake.cli.args import Context
from unbake.cli.output import Result

NAME = "publish"
HELP = "Land exact FILEs: prove the ROMs, write src/, commit and push."
DESCRIPTION = """\
Land each file whose function is exact in every version. For each one: build the ROM of every
holding version with the new C, compare it with the original, and only then write src/FUNC.c,
update layout.toml, commit "Match FUNC" and push. A file that does not prove is not written.

  unbake publish build/work/func_80012345/func_80012345.c
  unbake publish build/work/a/a.c build/work/b/b.c

Pushing needs [publish] in unbake.toml (remote, branch, author, credential).
"""
PROJECT = "ready"


def READ_ONLY(args: argparse.Namespace) -> bool:  # noqa: N802 - verb protocol
    return False


def register(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("files", type=Path, nargs="+", metavar="FILE")
    parser.add_argument("--no-push", action="store_true", help="Commit but leave pushing to a later run.")


def run(context: Context) -> Result:
    from unbake import land

    done = land.publish(
        context.project(), context.require_host(), [path.resolve() for path in context.args.files], push=not context.args.no_push
    )
    following = context.cmd("next")
    result = Result.ok(NAME, done.document(), done.lines(), following)
    if done.failed:
        return Result(NAME, "held", "land.mismatch", done.document(), following, tuple(done.lines()))
    return result
