"""Locate and validate the live project's progress layout."""

import re

from unbake.config import Held
from unbake.process import named as cause_named


def section(content: str) -> tuple[str, str, str]:
    headings = list(re.finditer(r"^## Progress\r?\n\r?\n", content, re.MULTILINE))
    if len(headings) != 1:
        raise Held(
            cause_named(
                "readme.Progress",
                "readme.Progress: exactly one heading required",
                owner="report.readme_layout",
                stage="report",
            )
        )
    heading = headings[0]
    body = content[heading.end() :]
    end = re.search(r"\r?\n## ", body)
    if end is None:
        raise Held(
            cause_named(
                "readme.Progress",
                "readme.Progress: following section missing",
                owner="report.readme_layout",
                stage="report",
            )
        )
    return content[: heading.end()], body[: end.start()], body[end.start() :]


def complete(block: str) -> bool:
    """Require both measures in every VERSION table and a full multi-VERSION summary."""
    tables = list(re.finditer(r"^\| ([\w-]+) \([^\n|]+ \|\r?$", block, re.MULTILINE))
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
