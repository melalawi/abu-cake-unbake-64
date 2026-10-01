"""Read constant ownership and migrate private function pools."""

import argparse
import json
from pathlib import Path

from unbake.cli.common import Subparsers, receipt
from unbake.project.config import Policy, Project


def register(phases: Subparsers) -> None:
    parser = phases.add_parser("rodata", phase="rodata", help="Inspect and migrate compiler constants.")
    verbs = parser.add_subparsers(dest="verb", required=True)
    owners = verbs.add_parser("owners", phase="rodata", help="Read existing ROM, split, and ELF ownership evidence.")
    owners.add_argument("--version", required=True, metavar="V")
    migration = verbs.add_parser("migrate", phase="rodata", help="Move constant storage in every VERSION.")
    migration.add_argument("function", nargs="?")
    migration.add_argument(
        "--all", action="store_true", help="Migrate every owned constant in every VERSION in one transaction."
    )
    migration.add_argument(
        "--report", type=Path, help="Write the bulk object, owner, refusal, and byte census as JSON."
    )
    migration.add_argument("--apply", action="store_true")
    migration.add_argument("--stage", action="store_true", help="Write edits for a later whole-project ROM check.")


def run(args: argparse.Namespace, project: Project, policy: Policy) -> bool:
    from unbake.layout import rodata_migrate, rodata_owners, split_apply

    if args.verb == "owners":
        print(json.dumps(rodata_owners.scan(project, args.version).document(), indent=2))
        return False
    if getattr(args, "all", False):
        from unbake.layout.rodata_bulk import migrate_all

        if args.function:
            raise ValueError("--all cannot be combined with a function")
        bulk = migrate_all(project)
        edits = bulk.edits
        if args.report is not None:
            args.report.write_text(json.dumps(bulk.document(), indent=2) + "\n")
    else:
        if not args.function:
            raise ValueError("migrate requires a function or --all")
        edits = rodata_migrate.migrate(project, args.function)
    print(split_apply.diff(edits), end="")
    if not args.apply:
        return receipt("rodata", [f"preview {len(edits)} file edits"])
    results = split_apply.apply(project, policy, edits, staged=args.stage)
    return receipt(
        "rodata",
        [f"{'OK' if r.ok else 'HELD'}: {r.version}: {r.sha1_line} log {r.log}" for r in results]
        or [f"staged {'all constants' if getattr(args, 'all', False) else args.function}: {len(edits)} edits"],
    )


def main(argv: list[str] | None = None) -> int:
    """Run the command module independently of the host command registry."""
    import os
    from pathlib import Path

    from unbake.cli.common import Parser
    from unbake.project import config
    from unbake.project.config import Held

    parser = Parser(prog="unbake")
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--policy", type=Path)
    register(parser.add_subparsers(dest="phase", required=True))
    try:
        args = parser.parse_args(argv)
        if args.policy is not None:
            os.environ["UNBAKE_POLICY"] = str(args.policy.expanduser().absolute())
        return int(run(args, config.load(args.project), config.load_policy()))
    except (Held, OSError, ValueError) as error:
        parser.exit(1, f"HELD(rodata): {error}\n")


if __name__ == "__main__":
    raise SystemExit(main())
