"""publish: land exact functions: ROM proof, write, and commit."""

from __future__ import annotations

import argparse
from pathlib import Path

from unbake.cli.args import Context
from unbake.cli.output import Result

NAME = "publish"
HELP = "Land exact FILEs: prove the ROMs, write src/ and commit."
DESCRIPTION = """\
Land each file whose function is exact in every version. For each one: build the ROM of every
holding version with the new C, compare it with the original, and only then write src/FUNC.c,
update layout.toml and commit "Match FUNC". A file that does not prove is not written.

  unbake publish build/work/func_80012345/func_80012345.c
  unbake publish build/work/a/a.c build/work/b/b.c
  unbake publish --original osInvalDCache

--original lands an original-asm function (one no configured compiler emits from C) as byte-exact
src/FUNC.s, records its rule in unbake-original-asm.json and commits "Original asm FUNC".
"""
PROJECT = "ready"


def READ_ONLY(args: argparse.Namespace) -> bool:
    return False


def register(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("files", type=Path, nargs="*", metavar="FILE")
    parser.add_argument(
        "--original", action="append", default=[], metavar="FUNC", help="Land an original-asm function as src/FUNC.s."
    )


def run(context: Context) -> Result:
    from unbake import land
    from unbake.config import Held

    if not context.args.files and not context.args.original:
        raise Held("publish", "publish: name a FILE or --original FUNC")
    done = land.publish(
        context.project(),
        context.require_host(),
        [path.resolve() for path in context.args.files],
        originals=tuple(context.args.original),
    )
    following = context.cmd("next")
    result = Result.ok(NAME, done.document(), done.lines(), following)
    if done.failed:
        key = next(iter(done.failed.values()))["key"]
        return Result(NAME, "held", key, done.document(), following, tuple(done.lines()))
    return result
