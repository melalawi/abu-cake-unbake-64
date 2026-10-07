"""Explicit, validated exclusions shared by next and draft."""

import json
import re
from pathlib import Path

from unbake.config import Held, Project
from unbake.layout import split
from unbake.process import capture
from unbake.process import named as cause_named

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
        raise Held(
            capture(
                error,
                cause=cause_named(
                    "exclusions.file",
                    f"exclusions.file: {path}: {error}",
                    owner="decomp.exclusions",
                    stage="exclusions",
                ),
            )
        ) from error
    if not isinstance(value, dict) or set(value) != {"schema", "functions"} or type(value["schema"]) is not int:
        raise Held(
            cause_named(
                "exclusions.schema",
                f"exclusions.schema: {path}: expected schema and functions",
                owner="decomp.exclusions",
                stage="exclusions",
            )
        )
    if value["schema"] != 1:
        raise Held(
            cause_named(
                "exclusions.schema",
                f"exclusions.schema: {path}: expected schema 1",
                owner="decomp.exclusions",
                stage="exclusions",
            )
        )
    names = value["functions"]
    if not isinstance(names, list) or any(
        not isinstance(n, str) or not re.fullmatch(r"[A-Za-z_]\w*", n) for n in names
    ):
        raise Held(
            cause_named(
                "exclusions.functions",
                f"exclusions.functions: {path}: expected C identifier list",
                owner="decomp.exclusions",
                stage="exclusions",
            )
        )
    if len(set(names)) != len(names):
        raise Held(
            cause_named(
                "exclusions.functions",
                f"exclusions.functions: {path}: duplicate function",
                owner="decomp.exclusions",
                stage="exclusions",
            )
        )
    rows = [row for version in project.versions for row in split.functions(project, version)]
    available = {name for row in rows for name in (row.name, *row.aliases)}
    if not explicit:
        from unbake.decomp.exclusion_identity import canonical

        names = canonical(project, names, rows)
    unknown = set(names) - available
    if unknown:
        raise Held(
            cause_named(
                "exclusions.function",
                f"exclusions.function: {path}: unknown {', '.join(sorted(unknown))}",
                owner="decomp.exclusions",
                stage="exclusions",
            )
        )
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


def publication_edit(project: Project, functions: set[str]) -> list[split.Edit]:
    """Release published items in the same transaction as their sources."""
    path = project.root / MANIFEST
    if not path.exists():
        return []
    load(project)
    rows = [row for version in project.versions for row in split.functions(project, version)]
    published = set(functions)
    for row in rows:
        if row.name in functions or functions.intersection(row.aliases):
            published.update((row.name, *row.aliases))
    before = path.read_text()
    value = json.loads(before)
    from unbake.decomp.exclusion_identity import canonical

    value["functions"] = [name for name in canonical(project, value["functions"], rows) if name not in published]
    after = json.dumps(value, indent=2) + "\n"
    return [split.Edit(path, before, after, project.versions)] if before != after else []
