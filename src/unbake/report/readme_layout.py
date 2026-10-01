"""Recover a project's complete progress layout from its committed README history."""

import re
import subprocess
from pathlib import Path

from unbake.project.config import Held


def section(content: str) -> tuple[str, str, str]:
    heading = "## Progress\n\n"
    if content.count(heading) != 1:
        raise Held("report", "readme.Progress: exactly one heading required")
    before, body = content.split(heading)
    end = body.find("\n## ")
    if end < 0:
        raise Held("report", "readme.Progress: following section missing")
    return before + heading, body[:end], body[end:]


def complete(block: str) -> bool:
    """Require both measures in every VERSION table and a full multi-VERSION summary."""
    tables = list(re.finditer(r"^\| ([\w-]+) \([^\n|]+ \|$", block, re.MULTILINE))
    if not tables:
        return False
    versions = [table[1] for table in tables]
    for index, table in enumerate(tables):
        end = tables[index + 1].start() if index + 1 < len(tables) else len(block)
        labels = re.findall(r"<code>([\w-]+) +\[", block[table.end() : end])
        if labels != ["bytes", "functions"]:
            return False
    summary = re.findall(r"<code>([\w-]+) +\[", block[: tables[0].start()])
    return summary == ["all", *versions] or (len(versions) == 1 and not summary)


def restore(content: str, root: Path) -> str:
    """Restore missing progress blocks without changing live text outside Progress."""
    before, block, after = section(content)
    if complete(block):
        return content
    history = subprocess.run(
        ["git", "-C", str(root), "log", "--format=%H", "--", "README.md"],
        capture_output=True,
        text=True,
        check=False,
    )
    if history.returncode:
        # Uncommitted projects created by init have no reference history yet.
        return content
    current_versions = re.findall(r"^\| ([\w-]+) \(", block, re.MULTILINE)
    for revision in history.stdout.splitlines():
        result = subprocess.run(
            ["git", "-C", str(root), "show", f"{revision}:README.md"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode:
            continue
        try:
            _, candidate, _ = section(result.stdout)
        except Held:
            continue
        versions = re.findall(r"^\| ([\w-]+) \(", candidate, re.MULTILINE)
        # A unique configured rename is reconciled by the figure renderer.
        if (
            complete(candidate)
            and len(versions) == len(current_versions)
            and len(set(versions) - set(current_versions)) <= 1
        ):
            return before + candidate + after
    return content
