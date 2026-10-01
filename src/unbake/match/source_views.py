"""Select active source lines while retaining declaration edit offsets."""

from __future__ import annotations

import re
import subprocess
import tempfile
from pathlib import Path

from unbake.layout.structs_parser import Parser
from unbake.match.common import held
from unbake.project.config import Policy, Project


def parsers(project: Project, policy: Policy, text: str, versions: tuple[str, ...]) -> list[Parser]:
    """Ask cpp to select branches without expanding tokens or changing edit spans."""
    if not re.search(r"^\s*#\s*(?:if|ifdef|ifndef|elif)\b", text, re.M):
        parser = Parser(text)
        parser.parse()
        return [parser]
    lines = text.splitlines(keepends=True)
    result = []
    with tempfile.TemporaryDirectory(prefix="match-view-") as temporary:
        source = Path(temporary) / "source.c"
        source.write_text(text)
        for version in versions:
            command = [
                str(policy.cpp),
                *(flag for flag in policy.cppflags if flag != "-P"),
                "-fdirectives-only",
                *(f"-I{root}" for root in project.include),
                *(f"-D{macro}" for macro in project.version(version).macros),
                str(source),
            ]
            completed = subprocess.run(command, cwd=project.root, capture_output=True, text=True)
            if completed.returncode:
                held(f"VERSION {version}: declaration preprocessing: {completed.stderr.strip()}")
            active: set[int] = set()
            current, number = "", 1
            for line in completed.stdout.splitlines():
                marker = re.match(r'^#\s+(\d+)\s+"([^"]+)"', line)
                if marker:
                    number, current = int(marker[1]), marker[2]
                    continue
                if line.strip() and current == str(source) and 1 <= number <= len(lines):
                    active.add(number - 1)
                number += 1
            view = "".join(
                line if index in active else "".join("\n" if char == "\n" else " " for char in line)
                for index, line in enumerate(lines)
            )
            parser = Parser(view)
            parser.parse()
            result.append(parser)
    return result
