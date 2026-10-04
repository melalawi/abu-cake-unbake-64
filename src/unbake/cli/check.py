"""check: build every version with the generated Makefile (no Python) and check the sources."""

from __future__ import annotations

import argparse

from unbake.cli.args import Context
from unbake.cli.output import Result

NAME = "check"
HELP = "Build every version with plain make (no Python) and check source rules."
DESCRIPTION = """\
Regenerate the build files if their inputs changed, then run `make check` in a clean environment
whose PATH is [tools].path from unbake.toml (Python must not be on it). Every version's ROM must equal
the original byte for byte. Also checks repository hygiene and the C source rules.

  unbake check
  unbake check --files-only     # hygiene and source rules only, no build
"""
PROJECT = "ready"


def READ_ONLY(args: argparse.Namespace) -> bool:
    return False


def register(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--files-only", action="store_true", help="Skip the build; check files and rules.")


def run(context: Context) -> Result:
    from unbake import build

    outcome = build.check(context.project(), context.require_host(), files_only=context.args.files_only)
    if outcome.ok:
        return Result.ok(NAME, outcome.document(), outcome.lines(), context.cmd("next"))
    return Result(NAME, "held", "check.failed", outcome.document(), context.cmd("next"), tuple(outcome.lines()))
