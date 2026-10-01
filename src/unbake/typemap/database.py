"""Rendered type context, submit feedback and content-pinned redraft marks."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from unbake.project.config import Held, Policy, Project
from unbake.typemap import storage


def load(project: Project, *, required: bool = True) -> dict[str, Any] | None:
    path = project.build / "types/database.json"
    if not path.is_file() and not required:
        return None
    value = storage.read(path, "types.database")
    storage.validate_identity(project, value, "types.database")
    if value.get("inputs_sha256") != storage.inputs(project, headers=True):
        raise Held("draft", "types.inputs_stale: run unbake map then unbake solve")
    map_path = project.build / "map/facts.json"
    if not map_path.is_file() or value.get("map_sha256") != storage.digest(map_path.read_bytes()):
        raise Held("draft", "types.inputs_stale: map changed; run unbake solve")
    for relative, digest in value.get("rendered_sha256", {}).items():
        path = project.root / relative
        if not path.is_file() or storage.digest(path.read_bytes()) != digest:
            raise Held("draft", f"types.inputs_stale: rendered header changed: {relative}")
    return value


def context(project: Project) -> str:
    value = load(project)
    assert value is not None
    lines = ['#include "shared/typemap.h"', '#include "shared/prototypes.h"']
    lines.extend("/* unknown: " + row.replace("*/", "* /") + " */" for row in value["unknown"])
    lines.extend("/* conflict: " + row["key"].replace("*/", "* /") + " */" for row in value["conflicts"])
    return "\n".join(lines) + "\n"


def redrafts(project: Project) -> dict[str, Any]:
    path = project.build / "types/redraft.json"
    if not path.is_file():
        return {}
    value = storage.read(path, "types.redraft")
    storage.validate_identity(project, value, "types.redraft")
    return dict(value.get("functions", {}))


def clear_redraft(project: Project, function: str, type_db_sha256: str) -> None:
    path = project.build / "types/database.json"
    if not path.is_file() or storage.digest(path.read_bytes()) != type_db_sha256:
        raise Held("draft", "types.redraft: database changed during draft")
    marks = redrafts(project)
    if function in marks and marks[function]["type_db_sha256"] == type_db_sha256:
        del marks[function]
        storage.write(
            project.build / "types/redraft.json", storage.encoded({**storage.identity(project), "functions": marks})
        )


def _semantic(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _semantic(row)
            for key, row in value.items()
            if key not in ("provenance", "versions", "declaration", "prototype")
        }
    if isinstance(value, list):
        return [_semantic(row) for row in value]
    return value


def publish(project: Project, value: dict[str, Any], previous: dict[str, Any]) -> None:
    if not project.include:
        raise Held("solve", "paths.include: required shared type destination")
    root = project.include[0]
    authored = [
        path
        for include in project.include
        for path in sorted(include.rglob("*.h"))
        if not storage.generated(project, path)
    ]
    # Refer to existing homes. No scalar typedef or SDK aggregate is copied.
    includes = []
    for path in authored:
        relative = next(
            path.relative_to(include).as_posix() for include in project.include if path.is_relative_to(include)
        )
        includes.append(f'#include "{relative}"')
    type_lines = ["#ifndef UNBAKE_TYPEMAP_H", "#define UNBAKE_TYPEMAP_H", *includes]
    for name, record in sorted(value["structs"].items()):
        if record["state"] == "unknown":
            type_lines.append(f"/* {name}: partial shape; common base {record.get('common_base')}; size unknown */")
    type_lines.extend(("#endif", ""))
    prototypes = ["#ifndef UNBAKE_PROTOTYPES_H", "#define UNBAKE_PROTOTYPES_H", '#include "typemap.h"']
    for _name, record in sorted(value["functions"].items()):
        if record["state"] == "known":
            prototypes.append(record["prototype"])
    for _name, record in sorted(value["globals"].items()):
        if record["state"] == "known" and record["declaration"]:
            prototypes.append(record["declaration"])
    prototypes.extend(("#endif", ""))
    outputs = {
        root / "shared/typemap.h": "\n".join(type_lines).encode(),
        root / "shared/prototypes.h": "\n".join(prototypes).encode(),
    }
    value["rendered_sha256"] = {
        str(path.relative_to(project.root)): storage.digest(content) for path, content in outputs.items()
    }
    database = project.build / "types/database.json"
    outputs[database] = storage.encoded(value)
    digest = storage.digest(outputs[database])
    changed: set[str] = set()
    for kind in ("functions", "globals", "structs", "arrays"):
        before = previous.get(kind, {})
        after = value[kind]
        changed.update(
            f"{kind}:{name}"
            for name in set(before) | set(after)
            if _semantic(before.get(name)) != _semantic(after.get(name))
        )
    marks = redrafts(project)
    if previous:
        for function, neighbours in value["dependencies"].items():
            reasons = []
            for entity in sorted(changed):
                kind, name = entity.split(":", 1)
                record = value[kind].get(name, {})
                old = previous.get(kind, {}).get(name, {})
                if (
                    (kind == "functions" and name in (function, *neighbours))
                    or function in record.get("users", [])
                    or function in old.get("users", [])
                ):
                    reasons.append(entity)
            if reasons:
                marks[function] = {
                    "function": function,
                    "reasons": reasons,
                    "revision": value["revision"],
                    "type_db_sha256": digest,
                }
    # Carry pending marks forward so a draft against the newest revision can clear them.
    for mark in marks.values():
        mark.update(revision=value["revision"], type_db_sha256=digest)
    outputs[project.build / "types/redraft.json"] = storage.encoded({**storage.identity(project), "functions": marks})
    before = {path: path.read_bytes() if path.is_file() else None for path in outputs}
    try:
        for path, content in outputs.items():
            storage.write(path, content)
    except BaseException:
        for path, content in before.items():
            if content is None:
                path.unlink(missing_ok=True)
            else:
                storage.write(path, content)
        raise


def feedback(
    project: Project,
    function: str,
    source: Path,
    *,
    versions: list[str],
    proof: dict[str, Any],
    policy: Policy | None = None,
) -> dict[str, Any]:
    """Accept actual published all-version matches, refresh facts, solve and mark users."""
    from unbake.typemap.mapping import map_program
    from unbake.typemap.solver import solve

    if not re.fullmatch(r"[A-Za-z_]\w*", function):
        raise Held("submit", "types.feedback.function: required C identifier")
    source = source.resolve()
    if not source.is_relative_to(project.src.resolve()) or not source.is_file():
        raise Held("submit", "types.feedback.source: published project C required")
    digest = storage.digest(source.read_bytes())
    for key in ("matched", "source_sha256", "versions", "target_sha256"):
        if key not in proof:
            raise Held("submit", f"types.feedback.{key}: missing proof")
    if proof["matched"] is not True or proof["source_sha256"] != digest:
        raise Held("submit", "types.feedback.source_sha256: exact matched source proof required")
    if not versions or set(versions) != set(proof["versions"]) or set(versions) != set(proof["target_sha256"]):
        raise Held("submit", "types.feedback.versions: every containing version must be proved")
    facts = map_program(project)
    item = facts["functions"].get(function)
    if item is None:
        item = next((row for row in facts["functions"].values() if function in row["aliases"]), None)
    if item is None or set(versions) != set(item["versions"]):
        raise Held("submit", "types.feedback.versions: proof differs from whole-program ownership")
    for version in versions:
        if proof["target_sha256"][version] != item["versions"][version]["target_sha256"]:
            raise Held("submit", f"types.feedback.target_sha256: {version}: ROM target differs")
        if item["versions"][version]["kind"] != "c":
            raise Held("submit", f"types.feedback.matched: {version}: function is not published C")
    path = project.build / "types/proven.json"
    previous = storage.read(path, "types.feedback") if path.is_file() else {**storage.identity(project), "records": {}}
    storage.validate_identity(project, previous, "types.feedback")
    previous["records"][function] = {
        "source": str(source.relative_to(project.root)),
        "source_sha256": digest,
        "versions": versions,
        "proof": proof,
        "rom_target_sha256": {v: item["versions"][v]["target_sha256"] for v in versions},
    }
    storage.write(path, storage.encoded(previous))
    if policy is None:
        from unbake.project.config import read_policy

        policy = read_policy()
    return solve(project, policy)
