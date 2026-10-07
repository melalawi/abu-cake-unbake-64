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

Then: put the ROM files in MyGame/roms/, set [build] asflags, cppflags and gnu_asflags in
MyGame/config.toml, and run `unbake setup` inside MyGame.

When no host config exists yet (--config FILE, $UNBAKE_CONFIG or ~/.config/unbake/unbake.toml),
init writes the full example there with every value commented out. An existing file is never touched.
Set [resources].domain = "standalone" to run alone, or an absolute broker manifest path,
and fill in the machine limits and tools before running setup.
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
    from unbake import config
    from unbake.project import init

    target = context.args.name.expanduser().absolute()
    lines = init.run(target, layout_cap=context.args.functions_per_header)
    host = config.host_path(context.config_path)
    if init.write_host_example(host):
        lines.append(f"host config example: {host} (uncomment and set every key)")
    return Result.ok(
        NAME,
        {"project": str(target), "roms": str(target / "roms")},
        lines,
        f"stop: put the ROM files in {target / 'roms'}, then run `unbake --project {target} setup`",
    )
