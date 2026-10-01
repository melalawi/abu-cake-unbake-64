"""The split command arguments and execution."""

import argparse
from pathlib import Path

from unbake.cli.common import Subparsers, integer, receipt
from unbake.project.config import Policy, Project


def register(phases: Subparsers) -> None:
    split = phases.add_parser("split", phase="split", help="Preview or apply split and symbol edits.")
    split_verbs = split.add_subparsers(dest="verb", required=True)
    for name in ("cut", "data-cut"):
        verb = split_verbs.add_parser(name, phase="split")
        verb.add_argument("function")
        verb.add_argument("--version", required=True, metavar="V")
        verb.add_argument("--start", type=integer, required=True, help="Inclusive ROM offset.")
        verb.add_argument("--end", type=integer, required=True, help="Exclusive ROM offset.")
        verb.add_argument("--apply", action="store_true")
    boundary_map = split_verbs.add_parser("boundary-map", phase="split", help="Apply a byte-pinned bulk boundary map.")
    boundary_map.add_argument("map", type=Path, help="JSON list of version/function/action/sha256/evidence records.")
    boundary_map.add_argument("--apply", action="store_true")
    classify = split_verbs.add_parser("classify", phase="split", help="Type measured in-text data runs.")
    classify.add_argument("--version", required=True, metavar="V")
    classify.add_argument("--apply", action="store_true")
    rename = split_verbs.add_parser("rename", phase="split")
    rename.add_argument("function")
    rename.add_argument("new_name")
    rename.add_argument("--apply", action="store_true")
    place = split_verbs.add_parser("place", phase="split")
    place.add_argument("function")
    place.add_argument("--version", required=True, metavar="V")
    place.add_argument("--address", type=integer, required=True, help="VRAM address.")
    place.add_argument("--apply", action="store_true")
    data = split_verbs.add_parser("data-symbol", phase="split", help="Add or rename a data symbol row.")
    data.add_argument("name")
    data.add_argument("--version", metavar="V")
    data.add_argument("--address", type=integer)
    data.add_argument("--rename-from")
    data.add_argument("--correspond", action="store_true", help="Infer placements in all VERSIONs from aligned code.")
    data.add_argument("--apply", action="store_true")
    twins = split_verbs.add_parser("twins", phase="split")
    twins.add_argument("function")
    twins.add_argument("--version", required=True, metavar="V")
    twins.add_argument("--apply", action="store_true")


def run(args: argparse.Namespace, project: Project, policy: Policy) -> bool:
    from unbake.layout import split_apply, split_edits, split_partition

    if args.verb == "boundary-map":
        from unbake.layout import boundary_map

        changes = boundary_map.read(args.map)
        edits = boundary_map.plan(project, changes)
        if not args.apply:
            print(split_apply.diff(edits), end="")
            return receipt("split", [f"preview {len(changes)} boundary changes in {len(edits)} VERSION splits"])
        results = boundary_map.apply(project, policy, changes)
        return receipt("split", [f"{result.version}: {result.sha1_line}" for result in results] or ["no edits"])
    if args.verb == "data-symbol":
        from unbake.decomp.symbols_edits import data_symbol
        from unbake.layout.data_symbols import correspondence
        from unbake.project.config import Held

        if args.correspond:
            if args.version is not None or args.address is not None or args.rename_from is not None:
                raise Held("split", "--correspond cannot be combined with explicit placement or rename")
            edits = correspondence(project, policy, args.name)
        else:
            if args.version is None or args.address is None:
                raise Held("split", "data-symbol requires --version and --address, or --correspond")
            edits = data_symbol(project, policy, args.version, args.name, args.address, args.rename_from)
    elif args.verb == "classify":
        edits = split_partition.classify(project, args.version)
    elif args.verb in ("cut", "data-cut"):
        operation = split_edits.cut if args.verb == "cut" else split_edits.data_cut
        edits = operation(project, args.version, args.function, args.start, args.end)
    elif args.verb == "rename":
        edits = split_edits.rename(project, args.function, args.new_name)
    elif args.verb == "place":
        edits = split_edits.place(project, args.version, args.function, args.address)
    else:
        edits = split_edits.twins(project, args.version, args.function)
    preview = split_apply.diff(edits)
    if preview:
        print(preview, end="" if preview.endswith("\n") else "\n")
    if not args.apply:
        receipt("split", [f"preview {len(edits)} file edits"])
        return False
    results = split_apply.apply(project, policy, edits)
    lines = []
    for result in results:
        if result.ok:
            lines.append(f"OK(split): {result.version}: {result.sha1_line}")
        else:
            lines.append(f"HELD(split): VERSION {result.version}: {result.sha1_line} log {result.log}")
    if not results:
        lines.append("no edits")
    return receipt("split", lines)
