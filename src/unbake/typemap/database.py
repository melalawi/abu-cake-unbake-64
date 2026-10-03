"""Rendered type context, submit feedback and content-pinned redraft marks."""

from __future__ import annotations

import os
import re
import shutil
import tempfile
from collections import defaultdict
from pathlib import Path
from typing import Any

from unbake.project.config import Held, Policy, Project
from unbake.typemap import header_names, split, storage

_decoded: dict[Path, tuple[tuple[int, int, int], dict[str, Any]]] = {}


def load(project: Project, *, required: bool = True, allow_stale: bool = False) -> dict[str, Any] | None:
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
    if allow_stale:
        return value
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


def context(project: Project, *, function: str | None = None, allow_stale: bool = False) -> str:
    value = load(project, allow_stale=allow_stale)
    assert value is not None
    homes = value.get("declaration_headers", {})
    selected = [homes[function]] if function in homes else sorted(homes.values())
    lines = [f'#include "{path}"' for path in selected]
    if not homes:
        root = project.include[0]
        if (root / "shared/typemap.h").is_file():
            lines = ['#include "shared/typemap.h"', '#include "shared/prototypes.h"']
        else:
            lines = [
                f'#include "{path.relative_to(root).as_posix()}"'
                for path in sorted((root / "shared/types").glob("*.h"))
            ]
    lines.extend("/* unknown: " + row.replace("*/", "* /") + " */" for row in value["unknown"])
    lines.extend("/* conflict: " + row["key"].replace("*/", "* /") + " */" for row in value["conflicts"])
    for name, record in sorted(value["functions"].items()):
        from unbake.typemap.abi_declarations import for_caller

        carrier = for_caller(record, function)
        if carrier.get("prototype"):
            reasons = "; ".join(carrier["reasons"]).replace("*/", "* /")
            prototype = header_names.rewrite(carrier["prototype"], value.get("shared_aliases", {}), set())
            lines.extend((f"/* {name}: {reasons} */", prototype))
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
    from unbake.decomp.header_declarations import declaration_source
    from unbake.decomp.header_declarations import declarations as header_declarations
    from unbake.typemap.declarations import declarator

    if not project.include:
        raise Held("solve", "paths.include: required shared type destination")
    root = project.include[0]
    authored = [
        path
        for include in project.include
        for path in sorted(include.rglob("*.h"))
        if not storage.generated(project, path)
    ]
    # Refer to existing homes; only missing generated prerequisites are emitted.
    consumer_names: dict[Path, set[str]] = {}
    reserved = header_names.source_names(project, root / "shared/typemap.h", policy, consumers=consumer_names)
    replacements = {"M2C_UNK": "s32", **{f"M2C_UNK{width}": f"s{width}" for width in (8, 16, 32, 64)}}
    components = {path: path.read_text() for path in authored}
    authored_aliases = {alias for text in components.values() for alias in header_declarations(text).typedefs}
    provided = set(authored_aliases)
    generated = {
        name: record
        for name, record in value["structs"].items()
        if record["state"] == "known"
        and (record.get("partial") or record.get("generated"))
        and record.get("declaration")
    }
    for text in components.values():
        replacements.update(
            (alias, type_)
            for alias, type_ in header_names.alias_types(text).items()
            if alias in reserved or header_names.placeholder(alias)
        )
    replacements.update(
        (alias, record["type"])
        for record in value["structs"].values()
        if record["state"] == "known"
        for alias in record.get("aliases", [])
        if alias in reserved
    )
    local_types = {
        name: {
            alias: type_
            for alias, type_ in record.get("typedefs", {}).items()
            if alias in reserved or header_names.placeholder(alias)
        }
        for name, record in generated.items()
    }
    evidence: dict[str, set[str]] = defaultdict(set)
    for types in local_types.values():
        for alias, type_ in types.items():
            evidence[alias].add(type_)
    replacements.update((alias, next(iter(types))) for alias, types in evidence.items() if len(types) == 1)
    # Resolve chains of source-owned aliases before removing their declarations.
    for _ in range(len(replacements)):
        expanded = {name: header_names.resolve(type_, replacements) for name, type_ in replacements.items()}
        if expanded == replacements:
            break
        replacements = expanded
    else:
        raise Held("solve", "types.header_parse: cyclic source-owned typedefs")
    generated_aliases = {alias for record in generated.values() for alias in record.get("aliases", [])}
    private: dict[str, str] = {}
    private_names: dict[tuple[str, str], str] = {}

    def shared_type(alias: str, type_: str) -> str:
        if "(" not in type_ and "[" not in type_:
            return type_
        key = (alias, type_)
        if key not in private_names:
            name = "UnbakeShared_" + alias
            while (
                name in reserved
                or name in authored_aliases
                or name in generated_aliases
                or name in replacements
                or name in private
            ):
                name += "_"
            private[name] = type_
            private_names[key] = name
        return private_names[key]

    concrete = dict(replacements)
    replacements = {alias: shared_type(alias, type_) for alias, type_ in concrete.items()}
    scoped = {}
    for name, types in local_types.items():
        scope = {**concrete, **types}
        # Prerequisite metadata is already canonical, but may still refer to a
        # source-owned aggregate alias. A same-named scalar or callback can have
        # different targets in different source scopes; preserve each target.
        scoped[name] = {
            **replacements,
            **{alias: shared_type(alias, header_names.resolve(type_, scope)) for alias, type_ in types.items()},
        }
    provided.update(generated_aliases)
    alias_targets = {
        alias: record["type"]
        for record in value["structs"].values()
        if record["state"] == "known"
        for alias in record.get("aliases", [])
    }
    prerequisites: dict[str, str] = dict(private)
    for record in generated.values():
        for alias, type_ in record.get("typedefs", {}).items():
            if alias in provided or alias in reserved or header_names.placeholder(alias):
                continue
            if alias in prerequisites and prerequisites[alias] != type_:
                raise Held("solve", f"types.header_parse: conflicting generated typedef {alias}")
            prerequisites[alias] = type_
    originals = dict(components)
    for path in authored:
        components[path] = header_names.rewrite(components[path], replacements, reserved)
    rendered = header_names.imports(project, originals, components)
    for alias, type_ in sorted(prerequisites.items()):
        path = root / "shared" / (".typedef-" + alias + ".h")
        components[path] = rendered[path] = header_names.rewrite(
            "typedef " + declarator(type_, alias) + ";", replacements, reserved
        )
    for name, record in sorted(value["structs"].items()):
        if (
            record["state"] == "known"
            and (record.get("partial") or record.get("generated"))
            and record.get("declaration")
        ):
            path = root / "shared" / (".layout-" + name + ".h")
            declaration = header_names.rewrite(record["declaration"], scoped[name], reserved)
            aliases = "".join(
                f"typedef {record['type']} {alias};\n"
                for alias in record.get("aliases", [])
                if alias not in authored_aliases and alias not in reserved and not header_names.placeholder(alias)
            )
            if aliases:
                alias_path = root / "shared" / (".aliases-" + name + ".h")
                components[alias_path] = rendered[alias_path] = aliases
            components[path] = rendered[path] = declaration
        elif record["state"] == "unknown":
            reason = record.get("reason", "layout evidence is incomplete").replace("*/", "* /")
            path = root / "shared" / (".layout-" + name + ".h")
            components[path] = ""
            rendered[path] = (
                f"/* {name}: partial shape; common base {record.get('common_base')}; size unknown; {reason} */"
            )
    # Source compatibility wrappers contain no declarations. They must not
    # become providers (or drag their old umbrella into a type header).
    wrappers = {
        path: originals[path]
        for path in authored
        if re.search(r'#\s*include\s*"(?:(?:shared/)?typemap.h|shared/(?:types|consumers)/[^"]+)"', originals[path])
    }
    for path in wrappers:
        if not header_declarations(components[path]).typedefs and not header_declarations(components[path]).exports:
            components.pop(path)
            rendered.pop(path)
        else:
            rendered[path] = split.narrow(components[path], "")
    consumer_aliases = {}
    for alias, type_ in {**value.get("typedefs", {}), **alias_targets}.items():
        if header_names.placeholder(alias) or (alias in authored_aliases and alias not in reserved):
            continue
        if alias not in reserved and (alias in generated_aliases or alias in prerequisites):
            continue
        path = root / "shared" / (".consumer-alias-" + alias + ".h")
        components[path] = rendered[path] = "typedef " + declarator(header_names.resolve(type_, concrete), alias) + ";"
        if alias in reserved:
            consumer_aliases[alias] = path
    layout = split.Layout(components, rendered, root, aliases=alias_targets)
    outputs: dict[Path, bytes | Path] = dict(layout.headers)
    umbrella_path = root / "shared/typemap.h"
    legacy = umbrella_path.is_file() or bool(wrappers)
    outputs[umbrella_path] = layout.umbrella(
        root / "shared/typemap.h", excluded={layout.homes[path] for path in consumer_aliases.values()}
    )
    declarations_by_name = {}
    for name, record in sorted(value["functions"].items()):
        if record["state"] == "known":
            prototype = header_names.rewrite(record["prototype"], replacements, reserved)
            declarations_by_name[name] = (
                prototype if prototype.startswith(("extern ", "static ")) else "extern " + prototype
            )
    for name, record in sorted(value["globals"].items()):
        if record["state"] == "known" and record["declaration"]:
            declarations_by_name[name] = header_names.rewrite(record["declaration"], replacements, reserved)
    for name, record in sorted(value["arrays"].items()):
        if record["state"] == "known" and record.get("partial") and ":" not in name:
            declarations_by_name[name] = header_names.rewrite(
                f"extern {record['type']} {name}[];", replacements, reserved
            )
    declaration_headers = []
    for name, text in declarations_by_name.items():
        path = root / "shared/decls" / (name + ".h")
        source = project.src / (name + ".c")
        record = value["functions"].get(name, {})
        selection = record.get("prototype", text)
        if source.is_file():
            selection += "\n" + source.read_text()
        homes = layout.required(selection, blocked=consumer_names.get(source, set()))
        outputs[path] = split.guarded(path, "\n".join(layout.include(home) for home in sorted(homes)) + "\n" + text)
        declaration_headers.append(layout.include(path))
    outputs[root / "shared/prototypes.h"] = split.guarded(root / "shared/prototypes.h", "\n".join(declaration_headers))
    if not legacy:
        outputs.pop(umbrella_path)
        outputs.pop(root / "shared/prototypes.h")
    source_context: dict[Path, str] = {}
    source_imports: dict[str, list[Path]] = defaultdict(list)
    for source in project.src.rglob("*.c"):
        text = source.read_text()
        source_context[source] = text
        for name in re.findall(r'^\s*#\s*include\s*"([^"\n]+)"', text, re.M):
            source_imports[Path(name).name].append(source)
    legacy_compat: dict[str, Path] = {}
    if legacy:
        for alias in ("M2C_UNK", "M2C_UNK8", "M2C_UNK16", "M2C_UNK32", "M2C_UNK64"):
            if any(
                alias in re.findall(r"\b[A-Za-z_]\w*\b", declaration_source(text))
                and alias not in consumer_names.get(source, set())
                for source, text in source_context.items()
            ):
                destination = root / "shared/consumers" / ("compat_" + alias + ".h")
                outputs[destination] = layout.consumer(
                    destination, "typedef " + declarator(replacements[alias], alias) + ";"
                )
                legacy_compat[alias] = destination

    def compatibility(source: Path, text: str) -> set[Path]:
        names = set(re.findall(r"\b[A-Za-z_]\w*\b", declaration_source(text))) - consumer_names.get(source, set())
        return {path for alias, path in legacy_compat.items() if alias in names}

    if legacy:
        alias_homes = {layout.homes[path] for path in consumer_aliases.values()}
        direct_branches: list[str] = []
        for source, text in sorted(source_context.items()):
            if not re.search(r'#\s*include\s*"shared/typemap.h"', text):
                continue
            selected = (layout.required(text, blocked=consumer_names.get(source, set())) & alias_homes) | compatibility(
                source, text
            )
            if not selected:
                continue
            destination = root / "shared/consumers" / (source.stem + ".h")
            outputs[destination] = split.guarded(
                destination, "\n".join(layout.include(home) for home in sorted(selected))
            )
            direct_branches.extend(
                (f"#if defined({split.consumer_macro(source.stem)})", layout.include(destination), "#endif")
            )
        umbrella = outputs[umbrella_path]
        assert isinstance(umbrella, bytes)
        outputs[umbrella_path] = ("\n".join(direct_branches) + "\n").encode() + umbrella
    for path, original in wrappers.items():
        branches: list[str] = []
        for source in sorted(set(source_imports[path.name])):
            text = source_context[source]
            homes = layout.required(
                text + "\n" + split.narrow(components.get(path, ""), ""), blocked=consumer_names.get(source, set())
            )
            homes.update(compatibility(source, text))
            destination = root / "shared/consumers" / (source.stem + ".h")
            includes = "\n".join(layout.include(home) for home in sorted(homes))
            # Several wrappers may contribute declarations to one consumer.
            previous_text = outputs.get(destination, b"")
            assert isinstance(previous_text, bytes)
            if previous_text:
                existing = re.findall(r'^#include "([^"]+)"', previous_text.decode(), re.M)
                homes.update(root / name for name in existing)
                includes = "\n".join(layout.include(home) for home in sorted(homes))
            outputs[destination] = split.guarded(destination, includes)
            directive = "#if" if not branches else "#elif"
            branches.extend((f"{directive} defined({split.consumer_macro(source.stem)})", layout.include(destination)))
        fallback = layout.required(split.narrow(components.get(path, ""), ""), blocked=reserved)
        if branches:
            branches.append("#else")
        branches.extend(layout.include(home) for home in sorted(fallback))
        if source_imports[path.name]:
            branches.append("#endif")
        outputs[path] = ("\n".join(branches) + "\n" + split.narrow(original, "")).encode()
    value["declaration_headers"] = {name: f"shared/decls/{name}.h" for name in declarations_by_name}
    value["shared_aliases"] = replacements
    abi_context = "\n".join(
        header_names.rewrite(record["abi_declaration"]["prototype"], replacements, reserved)
        for record in value["functions"].values()
        if record.get("abi_declaration", {}).get("prototype")
    )
    validated: set[str] = set()
    validate_headers(project, outputs, policy, abi_context=abi_context, validated=validated)
    variants = [record.get("abi_declaration", {}).get("variants", {}) for record in value["functions"].values()]
    for register in sorted({reg for choices in variants for reg in choices}):
        selected_context = "\n".join(
            header_names.rewrite(choices[register]["prototype"], replacements, reserved)
            for choices in variants
            if register in choices
        )
        validate_headers(
            project, outputs, policy, abi_context=abi_context + "\n" + selected_context, validated=validated
        )
    for path, content in outputs.items():
        relative = str(path.relative_to(project.root))
        if isinstance(content, bytes) and relative in value.get("inputs_sha256", {}):
            value["inputs_sha256"][relative] = storage.digest(content)
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
        dependants: dict[str, set[str]] = defaultdict(set)
        for function, neighbours in value["dependencies"].items():
            for name in (function, *neighbours):
                dependants[name].add(function)
        affected: dict[str, list[str]] = defaultdict(list)
        for entity in sorted(changed):
            kind, name = entity.split(":", 1)
            record = value[kind].get(name, {})
            old = previous.get(kind, {}).get(name, {})
            users = set(record.get("users", [])) | set(old.get("users", []))
            if kind == "functions":
                users.update(dependants.get(name, ()))
            for function in users:
                if function in value["dependencies"]:
                    affected[function].append(entity)
        for function, reasons in affected.items():
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
    obsolete = {
        path
        for directory in (root / "shared/types", root / "shared/decls", root / "shared/consumers")
        for path in directory.rglob("*.h")
        if path not in outputs
    }
    backups: dict[Path, Path | None] = {}
    try:
        for path in set(outputs) | obsolete:
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
        for path in obsolete:
            path.unlink()
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
    project: Project,
    outputs: dict[Path, bytes | Path],
    policy: Policy | None,
    *,
    abi_context: str = "",
    validated: set[str] | None = None,
) -> None:
    """Parse the staged shared context before any revision or header is published."""
    from dataclasses import replace

    from unbake.decomp.draft_context import preprocess_context
    from unbake.decomp.trial_compile import run_tool
    from unbake.typemap import declarations

    if validated is None:
        validated = set()
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
        if project.include[0] / "shared/typemap.h" in outputs:
            source.write_text('#include "shared/typemap.h"\n#include "shared/prototypes.h"\n')
        else:
            source.write_text(
                "".join(
                    f'#include "{path.relative_to(project.include[0]).as_posix()}"\n'
                    for path in outputs
                    if path.is_relative_to(project.include[0])
                )
            )
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
                    context_text = text + "\n" + abi_context
                    key = storage.digest(context_text.encode())
                    if key not in validated:
                        declarations.extract(context_text, {"kind": "declared"})
                        validated.add(key)
                else:
                    expanded = preprocess_context(source, staged_project, policy, version, "__unbake_validate_context")
                    context_text = expanded + "\n" + abi_context
                    key = storage.digest(context_text.encode())
                    if key in validated:
                        continue
                    context = scratch / "expanded.c"
                    context.write_text(context_text)
                    if not getattr(policy, "m2c", None):
                        if isinstance(policy, Policy):
                            raise Held("solve", "policy.m2c: required shared context parser")
                        declarations.extract(context_text, {"kind": "declared"})
                        validated.add(key)
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
                    validated.add(key)
            except Held as error:
                raise Held(
                    "solve", f"types.header_parse: {version}: shared/typemap.h, shared/prototypes.h: {error.reason}"
                ) from error
