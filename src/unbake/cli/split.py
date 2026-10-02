"""The split command arguments and execution."""

import argparse
from pathlib import Path

from unbake.cli.common import Subparsers, integer, receipt
from unbake.project.config import Policy, Project


def register(phases: Subparsers) -> None:
    split = phases.add_parser("split", phase="split", help="Preview or apply split and symbol edits.")
    split_verbs = split.add_subparsers(dest="verb", required=True)
    for name in ("cut", "data-cut", "code"):
        verb = split_verbs.add_parser(
            name,
            phase="split",
            help=("Carve referenced, control-flow-proved code from a data interval." if name == "code" else None),
        )
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
    rename.add_argument("function", nargs="?")
    rename.add_argument("new_name", nargs="?")
    rename.add_argument("--map", type=Path, help="JSON old->new names; simultaneous transaction with all-ROM proof.")
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
    port = split_verbs.add_parser("port", phase="port", help="Prove and port existing C rows into other versions.")
    port.add_argument("functions", nargs="*")
    port.add_argument("--from-version", required=True, metavar="V")
    port.add_argument("--version", action="append", required=True, metavar="V")
    port.add_argument("--measure", type=Path, metavar="CSV", help="Write candidate inventory without compiling.")
    port.add_argument("--all-identical", action="store_true")
    port.add_argument("--apply", action="store_true", help="Stage rows after target-version object proofs.")
    twins = split_verbs.add_parser("twins", phase="split")
    twins.add_argument("function")
    twins.add_argument("--version", required=True, metavar="V")
    twins.add_argument("--apply", action="store_true")


def run(args: argparse.Namespace, project: Project, policy: Policy) -> bool:
    from unbake.layout import split_apply, split_edits, split_partition

    if args.verb == "rename" and args.map is None and (args.function is None or args.new_name is None):
        from unbake.project.config import Held

        raise Held("split", "split.rename.names: supply FUNCTION NEW_NAME or --map JSON")
    if args.verb == "rename":
        from unbake.cli import gate_split

        return gate_split.run(args, project, policy)

    if args.verb == "boundary-map":
        from unbake.layout import boundary_map

        changes = boundary_map.read(args.map)
        edits = boundary_map.plan(project, changes)
        if not args.apply:
            print(split_apply.diff(edits), end="")
            return receipt("split", [f"preview {len(changes)} boundary changes in {len(edits)} VERSION splits"])
        results = boundary_map.apply(project, policy, changes)
        return receipt("split", [f"{result.version}: {result.sha1_line}" for result in results] or ["no edits"])
    if args.verb == "port":
        from unbake.layout import port
        from unbake.project.config import Held

        rows = port.candidates(project, args.from_version, args.version)
        if args.measure is not None:
            port.measure(args.measure, rows)
            return receipt("port", [f"measured {len(rows)} candidate version rows in {args.measure}"])
        if bool(args.functions) == bool(args.all_identical):
            raise Held("port", "select function names or --all-identical")
        if args.all_identical:
            rows = [row for row in rows if row.identity in ("identical", "relocations")]
        else:
            missing = set(args.functions) - {row.function for row in rows}
            if missing:
                raise Held("port", f"no source-C/target-ASM candidates: {', '.join(sorted(missing))}")
            rows = [row for row in rows if row.function in args.functions]
        return receipt("port", port.port(project, policy, rows, project.work, apply=args.apply))
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
    elif args.verb == "code":
        import json

        from unbake.layout.code_interval import prove

        edits = split_edits.code(project, args.version, args.function, args.start, args.end, policy=policy)
        print("Proved code: " + json.dumps(prove(project, args.version, args.start, args.end, policy), sort_keys=True))
    elif args.verb in ("cut", "data-cut"):
        operation = split_edits.cut if args.verb == "cut" else split_edits.data_cut
        edits = operation(project, args.version, args.function, args.start, args.end)
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
