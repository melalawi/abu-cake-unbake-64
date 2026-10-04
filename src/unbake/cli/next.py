"""Explain the next project action selected by the terminal resolver."""

import argparse

from unbake.cli import guidance
from unbake.cli.common import receipt, suggest
from unbake import config
from unbake.config import PendingProject


def run(args: argparse.Namespace, project: PendingProject) -> bool:
    if project.state != "ready":
        action = guidance.resolve(project.root)
        reason = "setup must publish a proved ready project before drafting"
    else:
        from unbake.cli.workflow import select

        action, reason = select(
            config.load(project.root),
            config.load_policy(),
            exclude=getattr(args, "exclude", None),
            new=getattr(args, "new", False),
        )
    if getattr(args, "new", False):
        reason = "--new: " + reason
    elif project.state == "ready":
        reason += "; next --new selects undrafted work"
    if getattr(args, "new", False) and project.state == "ready":
        action += "; to redraft: " + guidance.command(project.root, "draft") + " FUNCTION --redraft"
    suggest(action)
    return receipt("next", reason.splitlines())
