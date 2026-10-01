"""The setup command arguments and execution."""

import argparse
from pathlib import Path

from unbake.cli.common import Subparsers, receipt
from unbake.project.config import Held, Policy, Project


def register(phases: Subparsers) -> None:
    setup = phases.add_parser("setup", phase="setup", help="Verify inputs and generate the standalone build.")
    setup.add_argument("--new", type=Path, metavar="ROM")
    setup.add_argument(
        "--compilers", action="store_true", help="Verify project compilers and show optional registry entries."
    )
    setup.add_argument(
        "--supply",
        type=Path,
        metavar="DIR",
        help="Directory containing each VERSION ROM, located recursively by SHA-1, and pinned compiler files.",
    )


def run(args: argparse.Namespace, project: Project, policy: Policy) -> bool:
    from unbake.project import setup, toolchain

    if args.supply is not None:
        setup.restore_roms(project, args.supply)
        toolchain.supply(project, policy, args.supply)
    if args.compilers:
        lines = []
        for ident, spec in toolchain.registry().items():
            directory = policy.cache_root / "compilers" / ident
            optional = ident not in project.compilers
            label = "optional; " if optional else "required; "
            if optional and not directory.exists():
                lines.append(f"OK(setup): {ident}: optional; not installed")
                continue
            try:
                toolchain.verify(directory, spec)
            except Held as error:
                lines.append(f"HELD(setup): {ident}: {label}{error.reason}")
            else:
                lines.append(f"OK(setup): {ident}: {label}installed; pins verified")
        return receipt("setup", lines)
    return receipt("setup", setup.run(project, policy, new_rom=args.new))
