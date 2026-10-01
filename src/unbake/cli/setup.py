"""Setup input census before layout, compiler confirmation and proof."""

import argparse
import tomllib
from pathlib import Path

from unbake.cli.common import Subparsers, receipt
from unbake.project.config import Held, PendingProject


def register(phases: Subparsers) -> None:
    parser = phases.add_parser("setup", phase="setup", help="Inventory ROMs and prepare project setup.")
    parser.add_argument("--names-from", metavar="VERSION")
    parser.add_argument("--version-name", action="append", default=[], metavar="OLD=NEW")
    parser.add_argument("--version-order", metavar="V1,V2,...")
    parser.add_argument("--name", metavar="STEM")
    parser.add_argument("--title", metavar="TITLE")
    parser.add_argument("--compilers", action="store_true", help="Inspect compiler registry pins.")
    parser.add_argument("--supply", type=Path, metavar="DIR")


def pairs(values: list[str], flag: str) -> dict[str, str]:
    result = {}
    for value in values:
        if "=" not in value:
            raise Held("setup", f"{flag}: {value}: expected OLD=NEW")
        key, item = value.split("=", 1)
        if not key or not item or key in result:
            raise Held("setup", f"{flag}: {value}: empty or duplicate assignment")
        result[key] = item
    return result


def run(args: argparse.Namespace, project: PendingProject) -> bool:
    from unbake.project import census, config, flow, setup_config, toolchain

    if args.compilers:
        policy = config.load_policy(args.policy, stage="setup")
        lines = []
        for ident, spec in toolchain.registry().items():
            directory = policy.cache_root / "compilers" / ident
            if not directory.exists():
                lines.append(f"{ident}: not installed")
                continue
            try:
                toolchain.verify(directory, spec)
            except Held as error:
                lines.append(f"HELD(setup): {ident}: {error.reason}")
            else:
                lines.append(f"{ident}: installed; pins verified")
        return receipt("setup", lines)
    # Empty ROM refusal precedes policy requirements and template creation.
    census.candidates(project)
    print(f"OK(setup): ROM folder: {project.roms}")
    print(f"OK(setup): policy: {config.policy_path(args.policy)}")
    policy_census = config.load_policy(args.policy, stage="census")
    with (project.root / "config.toml").open("rb") as source:
        saved = tomllib.load(source)
    selected = args.names_from if args.names_from is not None else saved["project"].get("names_from")
    result = census.run(
        project,
        policy_census,
        names_from=selected,
        renames=pairs(args.version_name, "--version-name") if args.version_name else None,
        order=tuple(args.version_order.split(",")) if args.version_order is not None else None,
    )
    if project.state == "awaiting-roms":
        setup_config.write_facts(project, result, name=args.name, title=args.title)
    print(f"OK(setup): census: {result.manifest}")
    policy = config.load_policy(args.policy, stage="setup")
    layout = flow.plan_layout(project, result, policy)
    proposal = flow.propose_compilers(project, result, layout, policy)
    return receipt("setup", flow.complete_setup(project, result, layout, proposal, policy))
