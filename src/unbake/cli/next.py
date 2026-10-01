"""Explain the next project action selected by the terminal resolver."""

import argparse

from unbake.cli import guidance
from unbake.cli.common import receipt, suggest
from unbake.project import config
from unbake.project.config import PendingProject


def run(args: argparse.Namespace, project: PendingProject) -> bool:
    if project.state != "ready":
        action = guidance.resolve(project.root)
        reason = "setup must publish a proved ready project before drafting"
    else:
        from unbake.cli.workflow import select

        action, reason = select(config.load(project.root), config.load_policy())
    suggest(action)
    return receipt("next", [reason])
