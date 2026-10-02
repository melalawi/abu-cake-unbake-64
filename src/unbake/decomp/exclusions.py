"""Explicit, validated exclusions shared by next and draft."""

import json
import re
from pathlib import Path

from unbake.layout import split
from unbake.project.config import Held, Project

MANIFEST = "unbake-exclusions.json"


def load(project: Project, path: Path | None = None) -> set[str]:
    explicit = path is not None
    path = path if explicit else project.root / MANIFEST
    assert path is not None
    if not explicit and not path.exists():
        return set()
    try:
        original = path.read_bytes()
        value = json.loads(original)
    except (OSError, ValueError) as error:
        raise Held("exclusions", f"exclusions.file: {path}: {error}") from error
    if not isinstance(value, dict) or set(value) != {"schema", "functions"} or type(value["schema"]) is not int:
        raise Held("exclusions", f"exclusions.schema: {path}: expected schema and functions")
    if value["schema"] != 1:
        raise Held("exclusions", f"exclusions.schema: {path}: expected schema 1")
    names = value["functions"]
    if not isinstance(names, list) or any(
        not isinstance(n, str) or not re.fullmatch(r"[A-Za-z_]\w*", n) for n in names
    ):
        raise Held("exclusions", f"exclusions.functions: {path}: expected C identifier list")
    if len(set(names)) != len(names):
        raise Held("exclusions", f"exclusions.functions: {path}: duplicate function")
    rows = [row for version in project.versions for row in split.functions(project, version)]
    available = {name for row in rows for name in (row.name, *row.aliases)}
    if not explicit:
        from unbake.decomp.exclusion_identity import canonical, publish

        refreshed = canonical(project, names, rows)
        if refreshed != names:
            publish(project, path, original, refreshed)
            names = refreshed
    unknown = set(names) - available
    if unknown:
        raise Held("exclusions", f"exclusions.function: {path}: unknown {', '.join(sorted(unknown))}")
    excluded = set(names)
    # An excluded alias excludes the whole item, including aliases in other ROMs.
    while True:
        before = len(excluded)
        for row in rows:
            aliases = {row.name, *row.aliases}
            if aliases & excluded:
                excluded.update(aliases)
        if len(excluded) == before:
            return excluded
