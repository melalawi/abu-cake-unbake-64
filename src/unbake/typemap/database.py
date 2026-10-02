"""Rendered type context, submit feedback and content-pinned redraft marks."""

from __future__ import annotations

import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any

from unbake.project.config import Held, Policy, Project
from unbake.typemap import storage

_decoded: dict[Path, tuple[tuple[int, int, int], dict[str, Any]]] = {}


def load(project: Project, *, required: bool = True) -> dict[str, Any] | None:
    path = project.build / "types/database.json"
    if not path.is_file() and not required:
        return None
    try:
        stat = path.stat()
    except OSError as error:
        raise Held("draft", f"types.database: {path}: {error}") from error
    stamp = (stat.st_ino, stat.st_mtime_ns, stat.st_size)
    cached = _decoded.get(path)
    if cached is not None and cached[0] == stamp:
        value = cached[1]
    else:
        value = storage.read(path, "types.database")
        _decoded[path] = (stamp, value)
    storage.validate_identity(project, value, "types.database")
    if value.get("inputs_sha256") != storage.inputs(project, headers=True):
        raise Held("draft", "types.inputs_stale: run unbake map then unbake solve")
    map_path = project.build / "map/facts.json"
    if not map_path.is_file() or value.get("map_sha256") != storage.file_digest(map_path):
        raise Held("draft", "types.inputs_stale: map changed; run unbake solve")
    shard = value.get("map_shard")
    if shard is not None:
        if not isinstance(shard, str) or Path(shard).name != shard:
            raise Held("draft", "map.shards: invalid shard name in database")
        storage.verify_file(project.build / "map" / shard, value["map_shard_sha256"], "map.shards")
    supplement = value.get("abi_supplement")
    if supplement is not None:
        filename = supplement.get("path")
        if not isinstance(filename, str) or Path(filename).name != filename:
            raise Held("draft", "map.abi.path: invalid ABI supplement name")
        storage.verify_file(project.build / "map" / filename, supplement["sha256"], "map.abi")
    for row in value.get("constraints", []):
        if row.get("kind") == "shard":
            path = project.root / row["path"]
            if not path.resolve().is_relative_to((project.build / "types").resolve()):
                raise Held("draft", "types.constraints: shard is outside the generated type directory")
            storage.verify_file(path, row["sha256"], "types.constraints")
    for relative, digest in value.get("rendered_sha256", {}).items():
        path = project.root / relative
        if not path.is_file() or storage.digest(path.read_bytes()) != digest:
            raise Held("draft", f"types.inputs_stale: rendered header changed: {relative}")
    return value


def context(project: Project, *, function: str | None = None) -> str:
    value = load(project)
    assert value is not None
    lines = ['#include "shared/typemap.h"', '#include "shared/prototypes.h"']
    lines.extend("/* unknown: " + row.replace("*/", "* /") + " */" for row in value["unknown"])
    lines.extend("/* conflict: " + row["key"].replace("*/", "* /") + " */" for row in value["conflicts"])
    for name, record in sorted(value["functions"].items()):
        from unbake.typemap.abi_declarations import for_caller

        carrier = for_caller(record, function)
        if carrier.get("prototype"):
            reasons = "; ".join(carrier["reasons"]).replace("*/", "* /")
            lines.extend((f"/* {name}: {reasons} */", carrier["prototype"]))
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


def publish(project: Project, value: dict[str, Any], previous: dict[str, Any], *, policy: Policy | None = None) -> None:
    from unbake.decomp.draft_context import ordered_headers

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
    components = {path: path.read_text() for path in authored}
    rendered = {}
    for path in authored:
        relative = next(
            path.relative_to(include).as_posix() for include in project.include if path.is_relative_to(include)
        )
        rendered[path] = f'#include "{relative}"'
    for name, record in sorted(value["structs"].items()):
        if (
            record["state"] == "known"
            and (record.get("partial") or record.get("generated"))
            and record.get("declaration")
        ):
            path = root / "shared" / (".layout-" + name + ".h")
            components[path] = record["declaration"]
            rendered[path] = record["declaration"]
        elif record["state"] == "unknown":
            reason = record.get("reason", "layout evidence is incomplete").replace("*/", "* /")
            path = root / "shared" / (".layout-" + name + ".h")
            components[path] = ""
            rendered[path] = (
                f"/* {name}: partial shape; common base {record.get('common_base')}; size unknown; {reason} */"
            )
    type_lines = ["#ifndef UNBAKE_TYPEMAP_H", "#define UNBAKE_TYPEMAP_H"]
    type_lines.extend(rendered[path] for path in ordered_headers(components))
    type_lines.extend(("#endif", ""))
    prototypes = ["#ifndef UNBAKE_PROTOTYPES_H", "#define UNBAKE_PROTOTYPES_H", '#include "typemap.h"']
    for _name, record in sorted(value["functions"].items()):
        if record["state"] == "known":
            prototype = record["prototype"]
            prototypes.append(prototype if prototype.startswith(("extern ", "static ")) else "extern " + prototype)
    for _name, record in sorted(value["globals"].items()):
        if record["state"] == "known" and record["declaration"]:
            prototypes.append(record["declaration"])
    for name, record in sorted(value["arrays"].items()):
        if record["state"] == "known" and record.get("partial") and ":" not in name:
            prototypes.append(f"extern {record['type']} {name}[];")
    prototypes.extend(("#endif", ""))
    outputs: dict[Path, bytes | Path] = {
        root / "shared/typemap.h": "\n".join(type_lines).encode(),
        root / "shared/prototypes.h": "\n".join(prototypes).encode(),
    }
    abi_context = "\n".join(
        record["abi_declaration"]["prototype"]
        for record in value["functions"].values()
        if record.get("abi_declaration", {}).get("prototype")
    )
    validate_headers(project, outputs, policy, abi_context=abi_context)
    variants = [record.get("abi_declaration", {}).get("variants", {}) for record in value["functions"].values()]
    for register in sorted({reg for choices in variants for reg in choices}):
        selected = "\n".join(choices[register]["prototype"] for choices in variants if register in choices)
        validate_headers(project, outputs, policy, abi_context=abi_context + "\n" + selected)
    value["rendered_sha256"] = {
        str(path.relative_to(project.root)): storage.digest(content)
        for path, content in outputs.items()
        if isinstance(content, bytes)
    }
    database = project.build / "types/database.json"
    staged = storage.stage_json(database, value)
    outputs[database] = staged
    digest = storage.file_digest(staged)
    summary: dict[str, Any] = {**storage.identity(project), "revision": value["revision"], "database_sha256": digest}
    changed: set[str] = set()
    for kind in ("functions", "globals", "structs", "arrays"):
        before = previous.get(kind, {})
        after = value[kind]
        summary[kind] = {
            name: {"semantic_sha256": storage.digest(storage.encoded(_semantic(row))), "users": row.get("users", [])}
            for name, row in after.items()
        }
        for name in set(before) | set(after):
            old = before.get(name, {})
            old_digest = old.get("semantic_sha256") or storage.digest(storage.encoded(_semantic(old)))
            new_digest = summary[kind].get(name, {}).get("semantic_sha256")
            if old_digest != new_digest:
                changed.add(f"{kind}:{name}")
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
    outputs[project.build / "types/summary.json"] = storage.encoded(summary)
    # Carry pending marks forward so a draft against the newest revision can clear them.
    for mark in marks.values():
        mark.update(revision=value["revision"], type_db_sha256=digest)
    outputs[project.build / "types/redraft.json"] = storage.encoded({**storage.identity(project), "functions": marks})
    backups: dict[Path, Path | None] = {}
    try:
        for path in outputs:
            if path.is_file():
                descriptor, name = tempfile.mkstemp(prefix=".typemap-backup-", dir=path.parent)
                os.close(descriptor)
                backup_path = Path(name)
                backups[path] = backup_path
                shutil.copyfile(path, backup_path)
            else:
                backups[path] = None
        for path, content in outputs.items():
            if isinstance(content, Path):
                storage.install(path, content)
            else:
                storage.write(path, content)
    except BaseException:
        for path, backup in backups.items():
            if backup is None:
                path.unlink(missing_ok=True)
            else:
                os.replace(backup, path)
        raise
    finally:
        staged.unlink(missing_ok=True)
        for backup in backups.values():
            if backup is not None:
                backup.unlink(missing_ok=True)


def feedback_many(
    project: Project,
    entries: list[dict[str, Any]],
    *,
    policy: Policy | None = None,
) -> dict[str, Any]:
    """Validate every published receipt, then refresh mapped metadata and solve once."""
    from unbake.typemap.mapping import refresh_map
    from unbake.typemap.solver import solve

    if not entries:
        raise Held("submit", "types.feedback.entries: at least one published receipt required")
    checked = []
    seen = set()
    for entry in entries:
        for key in ("function", "source", "versions", "proof"):
            if key not in entry:
                raise Held("submit", f"types.feedback.{key}: missing receipt field")
        function = entry["function"]
        if not isinstance(function, str) or not re.fullmatch(r"[A-Za-z_]\w*", function) or function in seen:
            raise Held("submit", "types.feedback.function: required distinct C identifier")
        seen.add(function)
        source = Path(entry["source"]).resolve()
        if not source.is_relative_to(project.src.resolve()) or not source.is_file():
            raise Held("submit", "types.feedback.source: published project C required")
        digest = storage.file_digest(source)
        versions, proof = entry["versions"], entry["proof"]
        for key in ("matched", "source_sha256", "versions", "target_sha256"):
            if key not in proof:
                raise Held("submit", f"types.feedback.{key}: missing proof")
        if proof["matched"] is not True or proof["source_sha256"] != digest:
            raise Held("submit", "types.feedback.source_sha256: exact matched source proof required")
        if (
            not versions
            or len(versions) != len(set(versions))
            or set(versions) != set(proof["versions"])
            or set(versions) != set(proof["target_sha256"])
        ):
            raise Held("submit", "types.feedback.versions: every containing version must be proved")
        checked.append((function, source, digest, versions, proof))
    facts = refresh_map(project)
    inventory = getattr(facts["functions"], "inventory", facts["functions"])
    aliases = {alias: name for name, row in inventory.items() for alias in (name, *row.get("aliases", []))}
    records = {}
    for function, source, digest, versions, proof in checked:
        canonical = aliases.get(function)
        item = facts["functions"][canonical] if canonical is not None else None
        if item is None or set(versions) != set(item["versions"]):
            raise Held("submit", "types.feedback.versions: proof differs from whole-program ownership")
        for version in versions:
            if proof["target_sha256"][version] != item["versions"][version]["target_sha256"]:
                raise Held("submit", f"types.feedback.target_sha256: {version}: ROM target differs")
            if item["versions"][version]["kind"] != "c":
                raise Held("submit", f"types.feedback.matched: {version}: function is not published C")
        if storage.file_digest(source) != digest:
            raise Held("submit", f"types.feedback.source_sha256: published source changed: {source}")
        records[function] = {
            "source": str(source.relative_to(project.root)),
            "source_sha256": digest,
            "versions": versions,
            "proof": proof,
            "rom_target_sha256": {v: item["versions"][v]["target_sha256"] for v in versions},
        }
    path = project.build / "types/proven.json"
    previous = storage.read(path, "types.feedback") if path.is_file() else {**storage.identity(project), "records": {}}
    storage.validate_identity(project, previous, "types.feedback")
    previous["records"].update(records)
    storage.write(path, storage.encoded(previous))
    if policy is None:
        from unbake.project.config import read_policy

        policy = read_policy()
    return solve(project, policy)


def feedback(
    project: Project,
    function: str,
    source: Path,
    *,
    versions: list[str],
    proof: dict[str, Any],
    policy: Policy | None = None,
) -> dict[str, Any]:
    return feedback_many(
        project, [{"function": function, "source": source, "versions": versions, "proof": proof}], policy=policy
    )


def validate_headers(
    project: Project, outputs: dict[Path, bytes | Path], policy: Policy | None, *, abi_context: str = ""
) -> None:
    """Parse the staged shared context before any revision or header is published."""
    from dataclasses import replace

    from unbake.decomp.draft_context import preprocess_context
    from unbake.decomp.trial_compile import run_tool
    from unbake.typemap import declarations

    project.build.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".type-context-", dir=project.build) as temporary:
        scratch = Path(temporary)
        roots = []
        for index, root in enumerate(project.include):
            staged_root = scratch / str(index)
            shutil.copytree(root, staged_root)
            roots.append(staged_root)
            for path, content in outputs.items():
                if path.is_relative_to(root) and isinstance(content, bytes):
                    staged = staged_root / path.relative_to(root)
                    staged.parent.mkdir(parents=True, exist_ok=True)
                    staged.write_bytes(content)
        staged_project = replace(project, include=tuple(roots))
        source = scratch / "context.c"
        source.write_text('#include "shared/typemap.h"\n#include "shared/prototypes.h"\n')
        assembly = scratch / "validate.s"
        assembly.write_text(".text\nglabel __unbake_validate_context\n jr $ra\n nop\n")
        for version in project.versions:
            try:
                if policy is None:
                    text = declarations.headers(project, None, version)
                    text += "\n" + "\n".join(
                        declarations.clean(content.decode())
                        for content in outputs.values()
                        if isinstance(content, bytes)
                    )
                    declarations.extract(text + "\n" + abi_context, {"kind": "declared"})
                else:
                    expanded = preprocess_context(source, staged_project, policy, version, "__unbake_validate_context")
                    context = scratch / "expanded.c"
                    context.write_text(expanded + "\n" + abi_context)
                    if not getattr(policy, "m2c", None):
                        if isinstance(policy, Policy):
                            raise Held("solve", "policy.m2c: required shared context parser")
                        declarations.extract(expanded + "\n" + abi_context, {"kind": "declared"})
                        continue
                    run_tool(
                        [
                            str(policy.m2c),
                            "--context",
                            str(context),
                            "--function",
                            "__unbake_validate_context",
                            str(assembly),
                        ],
                        project.root,
                        "solve",
                    )
            except Held as error:
                raise Held(
                    "solve", f"types.header_parse: {version}: shared/typemap.h, shared/prototypes.h: {error.reason}"
                ) from error
