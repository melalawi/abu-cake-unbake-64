"""Solve project types, exposing every unknown and conflict."""

import argparse
from pathlib import Path

from unbake.cli import common
from unbake.layout.split import Edit
from unbake.project.config import Policy, Project, relative_text
from unbake.project_tools import atomic as atomic_files
from unbake.typemap import redrafts, solve, storage


def register(phases: common.Subparsers) -> None:
    parser = phases.add_parser("solve", phase="solve", help="Solve mapped signatures, globals and shared layouts.")
    parser.add_argument(
        "--declarations-needed",
        type=Path,
        nargs="+",
        metavar="FILE",
        help="Import authored declaration evidence needed by these C sources before solving.",
    )


def run(args: argparse.Namespace, project: Project, policy: Policy) -> bool:
    map_path = project.build / "map/facts.json"
    before = map_path.stat().st_mtime_ns if map_path.is_file() else None
    needed = tuple(getattr(args, "declarations_needed", None) or ())
    reports: list[dict[str, str]] = []
    edits: list[Edit] = []
    index_before = None
    if needed:
        from unbake.layout import index, split_apply
        from unbake.typemap import declaration_evidence

        edits, reports = declaration_evidence.plan_many(project, policy, needed)
        storage.write(
            project.build / "types/declaration-admission.json",
            storage.encoded({"schema": 1, "state": "planned", "records": reports}),
        )
        index_file = index.path(project)
        index_before = index_file.read_bytes() if index_file.is_file() else b""
        split_apply._write_staging(project, edits)
        index.update(project, {edit.path: edit.after for edit in edits})
    try:
        value = solve(project, policy)
    except BaseException as error:
        if reports:
            storage.write(
                project.build / "types/declaration-admission.json",
                storage.encoded(
                    {
                        "schema": 1,
                        "state": "held",
                        "reason": relative_text(project.root, str(error)),
                        "records": reports,
                    }
                ),
            )
        for edit in reversed(edits):
            if edit.before:
                storage.write(edit.path, edit.before.encode())
            else:
                edit.path.unlink(missing_ok=True)
        if index_before is not None:
            if index_before:
                storage.write(index_file, index_before)
            else:
                index_file.unlink(missing_ok=True)
        raise
    if value is None:
        common.suggest("unbake next")
        return common.receipt("solve", ["unchanged inputs; build/types/database.json is current"])
    if reports:
        storage.write(
            project.build / "types/declaration-admission.json",
            storage.encoded({"schema": 1, "state": "solved", "records": reports}),
        )
        for report in reports:
            print(f"declaration evidence: {report['status']}: {report['source']}: {report['reason']}")
    lines = [
        f"revision={value['revision']} unknown={len(value['unknown'])} conflicts={len(value['conflicts'])}; "
        "build/types/database.json"
    ]
    if map_path.is_file() and map_path.stat().st_mtime_ns != before:
        refresh = storage.read(map_path, "map.facts").get("refresh")
        if refresh:
            lines.append(
                f"map refresh: reused={refresh['reused']} rescanned={refresh['rescanned']} "
                f"seconds={refresh['seconds']} abi_upgrade={refresh['abi_upgrade']}"
            )
    for kind in ("functions", "globals", "structs", "arrays"):
        records = value[kind]
        lines.append(f"{kind}: known={sum(row['state'] == 'known' for row in records.values())} total={len(records)}")
    conflicts = [f"{row['key']}: {row.get('alternatives', row.get('reason', []))}" for row in value["conflicts"]]
    conflict_path = project.build / "types/conflicts.txt"
    conflict_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_files.text(conflict_path, "".join(line + "\n" for line in conflicts), encoding="utf-8")
    lines.append(f"conflict list: {conflict_path}; showing {min(5, len(conflicts))} of {len(conflicts)}")
    lines.extend(conflicts[:5])
    lines.append(f"redraft={len(redrafts(project))}; unknown details are retained in the database")
    common.suggest("unbake next")
    return common.receipt("solve", lines)
