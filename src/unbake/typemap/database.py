"""Rendered type context, submit feedback and content-pinned redraft marks."""

from __future__ import annotations

import os
import re
import tempfile
from collections import defaultdict
from pathlib import Path
from typing import Any

from unbake import inputs
from unbake.config import Held, Host, Project
from unbake import atomic as atomic_files
from unbake.typemap import header_names, regeneration, storage, types_db

def load(project: Project, *, required: bool = True, allow_stale: bool = False) -> dict[str, Any] | None:
    """The whole solution from build/types.sqlite (the solver and the headers step need all of it)."""
    path = types_db.path(project)
    if not path.is_file():
        if not required:
            return None
        raise Held("draft", f"types.database: {path} is missing; the types step builds it")
    value = types_db.read(path)
    storage.validate_identity(project, value, "types.database")
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
            shard_path = project.root / row["path"]
            if not shard_path.resolve().is_relative_to((project.build / "types").resolve()):
                raise Held("draft", "types.constraints: shard is outside the generated type directory")
            storage.verify_file(shard_path, row["sha256"], "types.constraints")
    return value


def digest(project: Project) -> str:
    """The content digest of the installed solution (what redraft marks pin)."""
    path = types_db.path(project)
    if not path.is_file():
        raise Held("types", f"types.database: {path} is missing; the types step builds it")
    return str(types_db.meta(path, "content_sha256"))


def context(project: Project, *, function: str | None = None, allow_stale: bool = False) -> str:
    """Draft context: the function's header, then only the prototypes it depends on (rows read on demand)."""
    from unbake.layout import index
    from unbake.typemap.abi_declarations import for_caller

    path = types_db.path(project)
    if not path.is_file():
        raise Held("types", f"types.database: {path} is missing; the types step builds it")
    lookup = index.load(project)
    homes = lookup["symbols"]
    selected = [homes[function]] if function in homes else sorted(set(homes.values()))
    lines = [f'#include "{home}"' for home in selected]
    lines.extend("/* unknown: " + row.replace("*/", "* /") + " */" for row in types_db.meta(path, "unknown"))
    lines.extend("/* conflict: " + row["key"].replace("*/", "* /") + " */" for row in types_db.meta(path, "conflicts"))
    aliases = types_db.meta(path, "shared_aliases")
    if function is None:
        names: set[str] = set(types_db.read(path)["functions"])
    else:
        neighbours = types_db.entries(path, "dependencies", [function]).get(function, [])
        names = {function, *neighbours}
    for name, record in sorted(types_db.entries(path, "functions", names).items()):
        carrier = for_caller(record, function)
        if carrier.get("prototype"):
            reasons = "; ".join(carrier["reasons"]).replace("*/", "* /")
            prototype = header_names.rewrite(carrier["prototype"], aliases, set())
            lines.extend((f"/* {name}: {reasons} */", prototype))
    return "\n".join(lines) + "\n"


def redrafts(project: Project) -> dict[str, Any]:
    path = types_db.path(project)
    return types_db.redrafts(path) if path.is_file() else {}


def clear_redraft(project: Project, function: str, type_db_sha256: str) -> None:
    path = types_db.path(project)
    if not path.is_file() or digest(project) != type_db_sha256:
        raise Held("draft", "types.redraft: database changed during draft")
    marks = redrafts(project)
    if function in marks and marks[function]["type_db_sha256"] == type_db_sha256:
        del marks[function]
        types_db.set_redrafts(path, marks)


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


def _render(
    project: Project, value: dict[str, Any], policy: Host | None, session: regeneration.Session
) -> dict[Path, bytes | Path]:
    from unbake.decomp.header_declarations import declaration_source
    from unbake.decomp.header_declarations import declarations as header_declarations
    from unbake.typemap.declarations import declarator

    if not project.include:
        raise Held("solve", "paths.include: required shared type destination")
    root = project.include[0]
    authored = list(session.authored)
    # Refer to existing homes; only missing generated prerequisites are emitted.
    consumer_names: dict[Path, set[str]] = {}
    reserved = session.source_names(consumer_names)
    replacements = {"M2C_UNK": "s32", **{f"M2C_UNK{width}": f"s{width}" for width in (8, 16, 32, 64)}}
    components = dict(session.authored)
    components.update({root / path: text for path, text in value.get("declaration_evidence", {}).items()})
    components.update({root / path: text for path, text in value.get("published_declarations", {}).items()})
    # Keep a complete installed layout required by an authored by-value member
    # when current machine inference no longer reconstructs that aggregate.
    # Retain it as declared context, never as a matched-function proof.
    from unbake.layout import index as layout_index
    from unbake.layout import redeclarations
    from unbake.typemap import split
    from unbake.typemap.declaration_evidence import _body

    needed = set()
    for text in session.sources.values():
        by_value = {
            match[1]
            for match in re.finditer(r"\b(?:struct|union)\s+(\w+)\s+\w+\s*(?=[;=,\[)])", declaration_source(text))
        }
        needed.update(by_value - redeclarations.local_tags(text))
    defined = set().union(*(redeclarations.local_tags(text) for text in components.values()))
    defined.update(
        name
        for name, record in value["structs"].items()
        if record.get("state") == "known" and record.get("declaration")
    )
    for path in sorted(layout_index.headers(project)):
        for statement in split.statements(_body(path.read_text())):
            names = redeclarations.local_tags(statement)
            if names & (needed - defined):
                digest = storage.digest(statement.encode())
                name = ".evidence_" + digest + ".h"
                body = f"/* unbake declaration evidence: evidence_{digest} */\n" + statement
                components[root / name] = body
                value.setdefault("declaration_evidence", {})[name] = body
                defined.update(names)
    authored_aliases = {alias for text in components.values() for alias in header_declarations(text).typedefs}
    authored_tags = {tag for text in components.values() for tag in header_declarations(text).tags}
    authored_declarations: dict[str, set[Path]] = defaultdict(set)
    for path, text in components.items():
        for name in header_declarations(text).declared:
            authored_declarations[name].add(path)
    provided = set(authored_aliases)
    source_owned_tags = set().union(*(redeclarations.local_tags(text) for text in session.sources.values()))
    generated = {
        name: record
        for name, record in value["structs"].items()
        if record["state"] == "known"
        and (record.get("partial") or record.get("generated"))
        and record.get("declaration")
        and name not in authored_tags
        and name not in source_owned_tags
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
    for alias, type_ in {**value.get("typedefs", {}), **alias_targets}.items():
        if alias not in provided and alias not in reserved and not header_names.placeholder(alias):
            prerequisites[alias] = header_names.resolve(type_, concrete)
    for record in generated.values():
        for alias, type_ in record.get("typedefs", {}).items():
            if alias in provided or alias in reserved or header_names.placeholder(alias):
                continue
            from unbake.typemap.declarations import canonical

            type_ = canonical(type_, concrete)
            if alias in prerequisites and canonical(prerequisites[alias], concrete) != type_:
                raise Held("solve", f"types.header_parse: conflicting generated typedef {alias}")
            prerequisites[alias] = type_
    for path in (
        *authored,
        *(root / name for name in value.get("declaration_evidence", {})),
        *(root / name for name in value.get("published_declarations", {})),
    ):
        components[path] = session.rewrite(components[path], replacements, reserved)
    rendered = dict(components)
    for name in value.get("declaration_evidence", {}):
        path = root / name
        import base64

        retained = base64.b64encode(value["declaration_evidence"][name].encode()).decode()
        rendered[path] = components[path] + f"\n/* unbake evidence input: {retained} */\n"
    for alias, type_ in sorted(prerequisites.items()):
        path = root / "shared" / (".typedef-" + alias + ".h")
        components[path] = rendered[path] = session.rewrite(
            "typedef " + declarator(type_, alias) + ";", replacements, reserved
        )
    for name, record in sorted(value["structs"].items()):
        if name in generated:
            path = root / "shared" / (".layout-" + name + ".h")
            declaration = session.rewrite(record["declaration"], scoped[name], reserved)
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
    from unbake.layout import index, map

    declarations_by_name = {}
    for name, record in sorted(value["functions"].items()):
        if record["state"] == "known":
            prototype = session.rewrite(record["prototype"], replacements, reserved)
            declarations_by_name[name] = (
                prototype if prototype.startswith(("extern ", "static ")) else "extern " + prototype
            )
    for name, record in sorted(value["globals"].items()):
        if record["state"] == "known" and record["declaration"]:
            declarations_by_name[name] = session.rewrite(record["declaration"], replacements, reserved)
    for name, record in sorted(value["arrays"].items()):
        if record["state"] == "known" and record.get("partial") and ":" not in name:
            declarations_by_name[name] = session.rewrite(f"extern {record['type']} {name}[];", replacements, reserved)
    # Source-local declarations own their C scope. A machine-derived shared
    # declaration must not replace a different authored call/storage contract.
    # Equal declarations may still be shared and stripped by layout apply.
    from unbake.layout import redeclarations

    signature = re.compile(
        r"(?P<prototype>^[ \t]*(?:[A-Za-z_]\w*[\s*]+)+(?P<name>[A-Za-z_]\w*)\s*\([^;{}]*\)\s*)\{",
        re.M,
    )
    retained_contracts = {
        root / path: header_declarations(text).declared
        for path, text in value.get("published_declarations", {}).items()
    }
    for source_path, text in session.sources.items():
        local: dict[str, list[str]] = {}
        for start, end in redeclarations.spans(text):
            for variant in redeclarations.variants(text[start:end]):
                for name in header_declarations(variant).declared | header_declarations(variant).typedefs:
                    local.setdefault(name, []).append(variant)
        definitions = set()
        for match in signature.finditer(declaration_source(text)):
            definitions.add(match["name"])
            local.setdefault(match["name"], []).append(match["prototype"].strip() + ";")
        local_aliases = {**value.get("typedefs", {}), **replacements, **redeclarations.aliases([text])}
        for name in local.keys() & declarations_by_name.keys():
            if (name != source_path.stem or name in definitions) and any(
                not redeclarations.equivalent(variant, declarations_by_name[name], local_aliases)
                for variant in local[name]
            ):
                declarations_by_name.pop(name)
        # Installed generated declarations are dependencies, not owners of a
        # source-local or defined contract. Conflicting local views stay local;
        # inference already records their disagreement as declaration evidence.
        for path, names in retained_contracts.items():
            if (
                path in components
                and path.relative_to(root).as_posix() not in value.get("published_homes", {})
                and any(
                    not redeclarations.equivalent(variant, components[path], local_aliases)
                    for name in local.keys() & names
                    for variant in local[name]
                )
            ):
                components.pop(path)
                rendered.pop(path, None)
    # A source-private tag is not a shared prototype-scope type. Publishing an
    # otherwise equal entry prototype can create a distinct parameter tag before
    # the source's own definition, or import its complete definition twice.
    for name, declaration in list(declarations_by_name.items()):
        if source_owned_tags & set(re.findall(r"\b(?:struct|union|enum)\s+(\w+)", declaration_source(declaration))):
            declarations_by_name.pop(name)
    for path in retained_contracts:
        if path in components and source_owned_tags & set(
            re.findall(r"\b(?:struct|union|enum)\s+(\w+)", declaration_source(components[path]))
        ):
            components.pop(path)
            rendered.pop(path, None)
    # An authored provider is already imported through the graph.
    for name in authored_declarations:
        declarations_by_name.pop(name, None)
    ownership = map.load(project)
    segments = symbol_segments(project)
    fixed_homes = {
        root / path: {root / home for home in homes}
        for path, homes in value.get("published_homes", {}).items()
        if root / path in components
    }
    layout = session.layout(
        components, rendered, root, alias_targets, ownership, declarations_by_name, segments, fixed_homes
    )
    session.consumer_names = consumer_names
    outputs: dict[Path, bytes | Path] = dict(layout.headers)
    outputs[index.path(project)] = index.encoded(layout.index)
    value["declaration_headers"] = dict(layout.index["symbols"])
    value["shared_aliases"] = replacements
    return outputs


def symbol_segments(project: Project) -> dict[str, str]:
    """Assign data declarations by the reference version's symbol intervals."""
    from unbake.layout import split

    version = project.version(project.names_from)
    segments = split.layout(version.split)[2]
    symbols = split.symbols(version.symbols)[1]
    result = {}
    for name, (address, _, _) in symbols.items():
        for segment in segments:
            if segment.fields.get("type") != "code" or segment.end is None:
                continue
            start = int(segment.fields["start"], 0)
            base = int(segment.fields["vram"], 0)
            if base <= address < base + segment.end - start:
                result[name] = split.plain(segment.fields.get("name", f"span_{start:X}"))
                break
    return result


def publish(project: Project, value: dict[str, Any], previous: dict[str, Any], *, policy: Host | None = None) -> None:
    from unbake.cache import memo, serialized

    if not project.include:
        raise Held("solve", "paths.include: required shared type destination")
    session = regeneration.Session(project, policy)
    outputs = session.render(value, lambda: _render(project, value, policy, session))
    replacements = value["shared_aliases"]
    reserved = session.reserved
    abi_context = "\n".join(
        session.rewrite(record["abi_declaration"]["prototype"], replacements, reserved)
        for record in value["functions"].values()
        if record.get("abi_declaration", {}).get("prototype")
    )
    validated: set[str] = set()
    variants = [record.get("abi_declaration", {}).get("variants", {}) for record in value["functions"].values()]
    registers = sorted({reg for choices in variants for reg in choices})
    if not registers:
        validate_headers(project, outputs, policy, abi_context=abi_context, validated=validated, session=session)
    for register in registers:
        selected_context = "\n".join(
            session.rewrite(choices[register]["prototype"], replacements, reserved)
            for choices in variants
            if register in choices
        )
        # Each variant includes the default ABI and all staged headers, so the
        # first batch also certifies the default without a separate compiler run.
        validate_headers(
            project,
            outputs,
            policy,
            abi_context=abi_context + "\n" + selected_context,
            validated=validated,
            session=session,
        )
    value["rendered_sha256"] = {
        storage.relative(project, path): storage.digest(content)
        for path, content in outputs.items()
        if isinstance(content, bytes)
    }
    database = types_db.path(project)
    digest = types_db.content_digest(value)
    summary: dict[str, Any] = {}
    changed: set[str] = set()
    for kind in ("functions", "globals", "structs", "arrays"):
        before = previous.get(kind, {})
        after = value[kind]

        def summarize(after: dict[str, Any] = after) -> dict[str, Any]:
            return {
                name: {
                    "semantic_sha256": storage.digest(storage.encoded(_semantic(row))),
                    "users": list(row.get("users", [])),
                }
                for name, row in after.items()
            }

        summary[kind] = memo(
            "typemap.summary." + kind,
            storage.digest(serialized("typemap.database." + kind, after)),
            summarize,
        )
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
    # Carry pending marks forward so a draft against the newest revision can clear them.
    for mark in marks.values():
        mark.update(revision=value["revision"], type_db_sha256=digest)
    staged, _ = types_db.stage(database, value, summary, marks)
    from unbake.layout import index

    obsolete = index.headers(project) - outputs.keys()
    outputs = {
        path: content
        for path, content in outputs.items()
        if not path.is_file()
        or (
            inputs.digest(path) != inputs.digest(content)
            if isinstance(content, Path)
            else path.read_bytes() != content
        )
    }
    backups: dict[Path, Path | None] = {}
    try:
        for path in set(outputs) | obsolete | {database}:
            if path.is_file():
                descriptor, name = tempfile.mkstemp(prefix=".typemap-backup-", dir=path.parent)
                os.close(descriptor)
                backup_path = Path(name)
                backups[path] = backup_path
                atomic_files.copyfile(path, backup_path)
            else:
                backups[path] = None
        for path, content in outputs.items():
            if isinstance(content, Path):
                storage.install(path, content)
            else:
                storage.write(path, content)
        for path in obsolete:
            path.unlink()
        types_db.install(database, staged)
    except BaseException:
        for path, backup in backups.items():
            if backup is None:
                path.unlink(missing_ok=True)
            else:
                atomic_files.publish(backup, path)
        raise
    finally:
        staged.unlink(missing_ok=True)
        for backup in backups.values():
            if backup is not None:
                backup.unlink(missing_ok=True)


def validate_headers(
    project: Project,
    outputs: dict[Path, bytes | Path],
    policy: Host | None,
    *,
    abi_context: str = "",
    validated: set[str] | None = None,
    session: regeneration.Session | None = None,
) -> None:
    """Parse the staged shared context before any revision or header is published."""
    from dataclasses import replace

    from unbake.decomp.draft_context import preprocess_context
    from unbake.process import run_tool
    from unbake.cache import Cache, key, memo
    from unbake.typemap import declarations

    def remembered_digest(data: bytes) -> str:
        return memo("typemap-validation-digest", data, lambda: storage.digest(data), keep=32768)

    cache = Cache(policy.cache_root if policy is not None else project.root / ".unbake/cache")
    environment = session.environment if session is not None else regeneration.environment(project, policy)
    authored = (
        session.authored
        if session is not None
        else {
            path: path.read_text()
            for root in project.include
            for path in root.rglob("*.h")
            if not storage.generated(project, path)
        }
    )
    bundle_key = key(
        environment,
        abi_context,
        *(part for path, text in authored.items() for part in (str(path), text)),
        *(part for path, content in outputs.items() if isinstance(content, bytes) for part in (str(path), content)),
    )
    if cache.get("typemap-validation", bundle_key) is not None:
        return
    try:
        contents, closures, abi = regeneration.validation_inputs(project, outputs, abi_context, authored=authored)
    except Held as error:
        raise Held("solve", f"types.header_parse: {error.reason}") from error
    digests = {path: remembered_digest(data) for path, data in contents.items()}
    certificates = session.certificates if session is not None else regeneration.Certificates(cache, environment)
    if validated is None:
        validated = set()
    rows: list[tuple[str, set[Path], str]] = []

    def signature(name: str, inputs: set[Path]) -> str:
        pinned = sorted((str(dep), digests[dep]) for dep in inputs)
        return memo(
            "typemap-validation-signature",
            (environment, name, tuple(pinned)),
            lambda: key(environment, name, *(part for pair in pinned for part in pair)),
            keep=32768,
        )

    for path, closure in closures.items():
        if path not in outputs:
            continue
        # Include-only indexes observe authored prerequisites; generated leaves
        # each carry their own certificates.
        body = re.sub(r"^[ \t]*#[^\n]*|/\*.*?\*/|//[^\n]*", "", contents[path].decode(), flags=re.M | re.S)
        inputs = closure if body.strip() else {path, *(dep for dep in closure if dep not in outputs)}
        rows.append((signature(str(path), inputs), closure, ""))
    rows.extend((signature(text, closure), closure, text) for text, closure in abi)
    for version in project.versions:
        pending: dict[str, tuple[set[Path], str]] = {}
        for input_key, closure, text in rows:
            content_key = version + ":" + input_key
            if content_key not in validated and not certificates.contains(content_key):
                pending[content_key] = closure, text
        if not pending:
            continue
        selected = {path for closure, _ in pending.values() for path in closure}
        texts = list(dict.fromkeys(text for _, text in pending.values() if text))
        project.build.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".type-context-", dir=project.build) as temporary:
            scratch = Path(temporary)
            roots = [scratch / str(index) for index in range(len(project.include))]
            for root, staged_root in zip(project.include, roots, strict=True):
                staged_root.mkdir()
                for path in selected:
                    prefix = str(root) + os.sep
                    if str(path).startswith(prefix):
                        staged = staged_root / str(path)[len(prefix) :]
                        staged.parent.mkdir(parents=True, exist_ok=True)
                        atomic_files.write(staged, contents[path])
            staged_project = replace(project, include=tuple(roots))
            source = scratch / "context.c"
            generated = selected.intersection(outputs)
            dependencies = {dep for path in generated for dep in closures[path] if dep != path}
            entry_points = sorted(generated - dependencies)
            if not entry_points:
                entry_points = sorted(generated)
            covered = {dep for path in entry_points for dep in closures[path]}
            entry_points.extend(sorted(selected - covered))
            atomic_files.text(
                source,
                "".join(
                    f'#include "{str(path)[len(str(root)) + 1 :]}"\n'
                    for path in entry_points
                    for root in project.include
                    if str(path).startswith(str(root) + os.sep)
                ),
            )
            assembly = scratch / "validate.s"
            atomic_files.text(assembly, ".text\nglabel __unbake_validate_context\n jr $ra\n nop\n")
            try:
                if policy is None:
                    expanded = "\n".join(declarations.clean(contents[path].decode()) for path in sorted(selected))
                else:
                    expanded = preprocess_context(source, staged_project, policy, version, "__unbake_validate_context")
                context_text = expanded + "\n" + "\n".join(texts)
                context_key = key(context_text)
                if context_key not in validated:
                    if policy is not None and getattr(policy, "m2c", None):
                        context = scratch / "expanded.c"
                        atomic_files.text(context, context_text)
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
                    else:
                        if isinstance(policy, Host):
                            raise Held("solve", "policy.m2c: required shared context parser")
                        declarations.extract(context_text, {"kind": "declared"})
                    validated.add(context_key)
                certificates.add(set(pending))
                validated.update(pending)
            except Held as error:
                raise Held("solve", f"types.header_parse: {version}: {error.reason}") from error

    def complete(path: Path) -> None:
        atomic_files.write(path, b"validated\n")

    cache.produce("typemap-validation", bundle_key, complete)
