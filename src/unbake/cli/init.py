"""The init command arguments and execution."""

import argparse
from pathlib import Path

from unbake.cli.common import Subparsers, receipt
from unbake.project.config import Held


def register(phases: Subparsers) -> None:
    init = phases.add_parser("init", phase="init", help="Create and prove an all-assembly project.")
    init.add_argument("target", type=Path, metavar="DIR")
    inputs = init.add_mutually_exclusive_group(required=True)
    inputs.add_argument("--rompath", type=Path, metavar="DIR")
    inputs.add_argument("--rom", type=Path, metavar="FILE")
    init.add_argument("--compiler", action="append", default=[])
    init.add_argument("--version-name", action="append", default=[])
    init.add_argument("--names-from")
    init.add_argument("--name")
    init.add_argument("--title")
    init.add_argument("--split", choices=("functions", "files"))
    init.add_argument("--supply", type=Path)


def pairs(values: list[str], flag: str, *, whole: bool = False) -> dict[str, str]:
    result = {}
    for value in values:
        if whole and "=" not in value:
            key, item = "*", value
        elif "=" in value:
            key, item = value.split("=", 1)
        else:
            raise Held("init", f"{flag} {value}: expected OLD=NEW")
        if not key or not item or key in result:
            raise Held("init", f"{flag} {value}: empty or duplicate assignment {key}")
        result[key] = item
    if "*" in result and len(result) > 1:
        raise Held("init", f"{flag}: whole-project and region choices conflict")
    return result


def run(args: argparse.Namespace) -> bool:
    from unbake.project import init

    compiler = pairs(args.compiler, "--compiler", whole=True)
    names = pairs(args.version_name, "--version-name")
    forced = init.Forced(
        compiler=compiler,
        version_names=names,
        names_from=args.names_from,
        name=args.name,
        title=args.title,
        split=args.split,
        supply=args.supply,
    )
    if args.rom is not None:
        roms = [args.rom]
    else:
        if not args.rompath.is_dir():
            raise Held("init", f"--rompath {args.rompath}: missing directory")
        roms = sorted(path for path in args.rompath.iterdir() if path.is_file())
        if not roms:
            raise Held("init", f"--rompath {args.rompath}: empty directory")
    return receipt("init", init.run(args.target, roms, forced))
