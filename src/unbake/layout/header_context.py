"""Validate and order installed header declarations before measuring layouts."""

import re
from pathlib import Path

from unbake.decomp.draft_context import ordered_headers
from unbake.layout.structs import Layout
from unbake.layout.structs_parser import Parser
from unbake.project.config import Held


def context(contents: dict[Path, str], *, root: Path | None = None) -> tuple[dict[Path, str], Parser, list[Layout]]:
    """Reuse draft's declaration parser; retain raw spans for header edits."""
    try:
        contents = {path: contents[path] for path in ordered_headers(contents)}
        parser = Parser("\n".join(contents.values()))
        records = parser.parse()
    except Held as error:
        reason = error.reason
        if root is not None:
            for path in contents:
                if path.is_relative_to(root):
                    reason = reason.replace(str(path), path.relative_to(root).as_posix())
        line = re.search(r"\bline (\d+)", reason)
        if line is not None:
            number = int(line[1])
            for path, text in contents.items():
                lines = text.count("\n") + 1
                if number <= lines:
                    label = path.relative_to(root) if root is not None and path.is_relative_to(root) else path
                    reason = reason[: line.start()] + f"{label}:{number}" + reason[line.end() :]
                    break
                number -= lines
        raise Held("structs", f"headers.declaration: SDK/shared header prerequisite: {reason}") from error
    return contents, parser, records
