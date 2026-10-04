"""Choose shared declaration homes and render aggregate declarations."""

from __future__ import annotations

import re
from pathlib import Path

from unbake.layout import split_apply
from unbake.layout.structs import Layout, held
from unbake.layout.structs_parser import Parser
from unbake.config import Project


def home(project: Project) -> Path:
    """Use an existing structs.h, or create it in the first configured include root."""
    if not project.include:
        held("project.include", "missing shared declaration home")
    candidates = [Path(root) / "structs.h" for root in project.include]
    return next((path for path in candidates if path.is_file()), candidates[0])


def declaration(record: Layout) -> str:
    members = "\n    ".join(field.declaration.strip() for field in record.fields)
    text = f"{record.kind} {record.name} {{\n    {members}\n}};\n"
    return text + "".join(f"typedef {record.kind} {record.name} {alias};\n" for alias in record.aliases)


def append(text: str, declarations: str) -> str:
    """Insert inside an existing header guard, preserving the existing text."""
    closings = list(re.finditer(r"^\s*#\s*endif\b[^\n]*", text, re.M))
    position = closings[-1].start() if closings and re.search(r"#\s*ifndef\b", text) else len(text)
    return text[:position].rstrip() + "\n\n" + declarations + text[position:]


def consolidate(project: Project) -> None:
    """Move aggregate-only function headers into the shared declaration home."""
    destination = home(project)
    text = (
        destination.read_text()
        if destination.exists()
        else ('#ifndef UNBAKE_STRUCTS_H\n#define UNBAKE_STRUCTS_H\n#include "types.h"\n\n#endif\n')
    )
    known = {record.name: record for record in Parser(text).parse()}
    moved: list[Path] = []
    for root in project.include:
        for path in sorted(Path(root).glob("func_*.h")):
            if not re.fullmatch(r"func_[0-9A-Fa-f]{8}(?:_fields|_declarations)?", path.stem):
                continue
            content = path.read_text()
            records = Parser(content).parse()
            if not records:
                continue
            remainder = content
            for record in reversed(records):
                remainder = remainder[: record.start] + remainder[record.end :]
            remainder = re.sub(r"/\*.*?\*/|//[^\n]*", "", remainder, flags=re.S)
            remainder = re.sub(r"^\s*#.*$", "", remainder, flags=re.M)
            if remainder.strip("; \t\r\n"):
                continue
            if path.is_symlink() or destination.is_symlink():
                held(str(path), "shared declaration files must not be symlinks")
            for record in records:
                if record.name in known:
                    if declaration(known[record.name]) != declaration(record):
                        held(record.name, "conflicting shared declaration")
                else:
                    text = append(text, declaration(record))
                    known[record.name] = record
            moved.append(path)
    if not moved:
        return
    Parser(text).parse()
    replacements = {path.name: destination.name for path in moved}
    edits: dict[Path, str] = {destination: text}
    for root in (*project.include, project.src):
        for path in sorted(Path(root).rglob("*")):
            if path.suffix not in (".h", ".c") or path in moved or path == destination:
                continue
            before = path.read_text()
            after = re.sub(
                r'(^\s*#\s*include\s*")([^"\n]+)(")',
                lambda match: match[1] + replacements.get(match[2], match[2]) + match[3],
                before,
                flags=re.M,
            )
            if after != before:
                if path.is_symlink():
                    held(str(path), "include source must not be a symlink")
                edits[path] = after
    for path, content in edits.items():
        split_apply.write(path, content)
    for path in moved:
        path.unlink()
