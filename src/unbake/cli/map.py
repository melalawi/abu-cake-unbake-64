"""Map all containing versions with ROM-pinned instruction provenance."""

import argparse

from unbake.cli import common
from unbake.project.config import Policy, Project
from unbake.typemap import map_program


def register(phases: common.Subparsers) -> None:
    phases.add_parser("map", phase="map", help="Map calls, registers and memory across every ROM.")


def run(args: argparse.Namespace, project: Project, policy: Policy) -> bool:
    facts = map_program(project)
    for version in project.versions:
        count = calls = memory = 0
        for item in facts["functions"].values():
            if version in item["versions"]:
                body = item["versions"][version]
                count += 1
                calls += len(body["calls"])
                memory += len(body["memory"])
        common.receipt(
            "map",
            [f"{version}: functions={count} calls={calls} memory={memory}"],
        )
    common.suggest("unbake solve")
    return common.receipt(
        "map", [f"items={len(facts['functions'])} globals={len(facts['globals'])}; build/map/facts.json"]
    )
