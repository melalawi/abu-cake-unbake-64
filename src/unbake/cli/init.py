"""init: create an empty project folder."""

from __future__ import annotations

import argparse
from pathlib import Path

from unbake.cli.args import Context, positive
from unbake.cli.output import Result

NAME = "init"
HELP = "Create a new project folder with an empty roms/ directory."
DESCRIPTION = """\
Create a new project folder (a git repository) with config.toml, layout.toml and roms/.

Example:
  unbake init MyGame --functions-per-header 32

Then: put the ROM files in MyGame/roms/, set [build] asflags, cppflags and sn64_asflags in
MyGame/config.toml, and run `unbake setup` inside MyGame.
"""
PROJECT = "none"


def READ_ONLY(args: argparse.Namespace) -> bool:
    return True


def register(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("name", type=Path, metavar="NAME", help="Folder to create; it must not exist or be empty.")
    parser.add_argument(
        "--functions-per-header", type=positive, required=True, metavar="N", help="Most functions per generated header."
    )


def run(context: Context) -> Result:
    from unbake.project import init

    target = context.args.name.expanduser().absolute()
    lines = init.run(target, layout_cap=context.args.functions_per_header)
    return Result.ok(
        NAME,
        {"project": str(target), "roms": str(target / "roms")},
        lines,
        f"stop: put the ROM files in {target / 'roms'}, then run `unbake --project {target} setup`",
    )
