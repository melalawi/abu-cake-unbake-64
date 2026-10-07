"""Rendered type context, submit feedback and content-pinned redraft marks."""

from __future__ import annotations

import json
import os
import re
import tempfile
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from unbake import atomic as atomic_files
from unbake import cache as retention
from unbake import effort, pool, tui
from unbake import inputs as input_pins
from unbake.config import Held, Host, Project
from unbake.typemap import header_names, namespace, regeneration, storage, types_db


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


def source_private(declaration: str, tags: set[str], typedefs: set[str]) -> bool:
    """True when DECLARATION spells a tag or typedef that only a source defines, so it cannot move to a header."""
    from unbake.cdecl import declaration_source

    code = declaration_source(declaration)
    ordinary = re.sub(r"\b(?:struct|union|enum)\s+\w+", "", code)
    return bool(
        tags & set(re.findall(r"\b(?:struct|union|enum)\s+(\w+)", code))
        or typedefs & set(re.findall(r"\b[A-Za-z_]\w*\b", ordinary))
    )


def _render(
    project: Project, value: dict[str, Any], policy: Host | None, session: regeneration.Session
) -> dict[Path, bytes | Path]:
    from unbake.cdecl import declaration_source
    from unbake.cdecl import declarations as header_declarations
    from unbake.typemap.declarations import declarator

    if not project.include:
        raise Held("solve", "paths.include: required shared type destination")
    root = project.include[0]
    authored = list(session.authored)
    # Refer to existing homes; only missing generated prerequisites are emitted.
    consumer_names: dict[Path, set[str]] = {}
    reserved = session.source_names(consumer_names)
    replacements = {"M2C_UNK": "s32", **{f"M2C_UNK{width}": f"s{width}" for width in (8, 16, 32, 64)}}
    # Retention follows installed C dependencies independently of the current
    # inference facts, including typedefs used only inside function bodies.
    retained_components, retained_homes = session.published, session.published_homes
    published = value.setdefault("published_declarations", {})
    published_homes = value.setdefault("published_homes", {})
    from unbake.layout.header_loss import declared

    # Installed contracts are canonical. Replace a stored copy of the same
    # declaration when the installed spelling changed, while keeping stored
    # providers for declarations absent from a partial installed tree.
    installed_paths = {path.relative_to(root).as_posix() for path in retained_components}
    installed_names = set().union(*(declared(text) for text in retained_components.values()))
    for name, text in list(published.items()):
        if name not in installed_paths and declared(text) & installed_names:
            published.pop(name)
            published_homes.pop(name, None)
    for path, text in retained_components.items():
        relative = path.relative_to(root).as_posix()
        published[relative] = text
        published_homes[relative] = sorted(home.relative_to(root).as_posix() for home in retained_homes[path])
    from unbake.typemap.declaration_evidence import validate_published

    contracts = getattr(session, "function_declarations", None)
    if contracts is None:
        contracts = namespace.FunctionDeclarations(
            value, {**session.authored, **{root / path: text for path, text in published.items()}}
        )
    for field in ("published_declarations", "declaration_evidence"):
        for path, text in value.get(field, {}).items():
            value[field][path] = contracts.rewrite(text)
    validate_published(
        project,
        value,
        published,
        context=tuple(contracts.rewrite(text) for text in session.authored.values()),
        policy=policy,
    )
    components = {path: contracts.rewrite(text) for path, text in session.authored.items()}
    components.update({root / path: text for path, text in value.get("declaration_evidence", {}).items()})
    components.update({root / path: text for path, text in value.get("published_declarations", {}).items()})
    namespace.check(value, components)
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
        try:
            installed = split.statements(_body(path.read_text()))
        except Held as error:
            raise Held(
                "solve",
                f"{error.reason} in {storage.relative(project, path)}",
                next_action=f"stop: repair or restore {storage.relative(project, path)} (a generated header is "
                "retained layout evidence), then run the command again",
            ) from error
        for statement in installed:
            names = redeclarations.local_tags(statement)
            if names & (needed - defined):
                digest = input_pins.bytes_digest(statement.encode(), algorithm="sha256")
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
        if alias not in authored_aliases
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
    # A (void) prototype refuses the arguments mapped callers pass (K&R calls): any header spelling of such a
    # function, rendered or carried, declares no parameter list instead.
    passed = unprototyped_calls(value["functions"])
    void_pattern = void_calls(passed)
    for name in passed & declarations_by_name.keys():
        declarations_by_name[name] = without_void(declarations_by_name[name], void_pattern)
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

    retained_contracts = {
        root / path: header_declarations(text).declared
        for path, text in value.get("published_declarations", {}).items()
    }
    source_typedefs: set[str] = set()
    homes = value.get("published_homes", {})
    published_homes = {path for path in retained_contracts if path.relative_to(root).as_posix() in homes}
    # Each source checks only the retained contracts declaring a name it declares locally (not every contract).
    contracts_by_name: dict[str, list[Path]] = defaultdict(list)
    for path, names in retained_contracts.items():
        for name in names:
            contracts_by_name[name].append(path)
    shared = _Drops(
        declarations_by_name,
        components,
        contracts_by_name,
        retained_contracts,
        published_homes,
        value.get("typedefs", {}),
        replacements,
        project,
        policy,
    )
    items = list(session.sources.items())
    decisions = (
        [_source_drops(shared, item) for item in items]
        if policy is None
        else pool.run(policy, _source_drops, items, shared)
    )
    # Each decision read the original values, so the order they are applied in does not matter.
    for typedefs, names, paths in decisions:
        source_typedefs |= typedefs
        for name in names:
            declarations_by_name.pop(name, None)
        for path in paths:
            components.pop(path, None)
            rendered.pop(path, None)
    # A source-private tag is not a shared prototype-scope type. Publishing an
    # otherwise equal entry prototype can create a distinct parameter tag before
    # the source's own definition, or import its complete definition twice.
    # Likewise a typedef only a source defines: a shared declaration spelling it cannot parse before that source.
    header_typedefs = authored_aliases.union(*(header_declarations(text).typedefs for text in components.values()))
    source_owned_typedefs = source_typedefs - header_typedefs
    for name, declaration in list(declarations_by_name.items()):
        if source_private(declaration, source_owned_tags, source_owned_typedefs):
            declarations_by_name.pop(name)
    # An authored provider is already imported through the graph.
    for name in authored_declarations:
        declarations_by_name.pop(name, None)
    one_declaration(
        declarations_by_name, {path: names for path, names in retained_contracts.items() if path in components}
    )
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


_SIGNATURE = re.compile(
    r"(?P<prototype>^[ \t]*(?:[A-Za-z_]\w*[\s*]+)+(?P<name>[A-Za-z_]\w*)\s*\([^;{}]*\)\s*)\{",
    re.M,
)


@dataclass(frozen=True)
class _Drops:
    """What every source is checked against: the rendered declarations and the contracts other headers retain."""

    declarations_by_name: dict[str, str]
    components: dict[Path, str]
    contracts_by_name: dict[str, list[Path]]
    retained_contracts: dict[Path, set[str]]
    published_homes: set[Path]
    typedefs: dict[str, str]
    replacements: dict[str, str]
    project: Project | None = None
    policy: Host | None = None


def _source_drops(shared: _Drops, item: tuple[Path, str]) -> tuple[set[str], set[str], set[Path]]:
    """Pool worker: one source's typedefs, the generated declarations it owns locally (names to drop) and the
    retained contracts it disagrees with (paths to drop). Mutates nothing."""
    from unbake.layout import redeclarations
    from unbake.typemap.declarations import source_definition_units

    source_path, text = item
    source_typedefs: set[str] = set()
    drop_names: set[str] = set()
    drop_paths: set[Path] = set()
    local: dict[str, list[str]] = {}
    for start, end in redeclarations.spans(text):
        for variant in redeclarations.variants(text[start:end]):
            parsed = redeclarations.parse(source_path, variant)
            source_typedefs |= parsed.typedefs
            for name in parsed.declared | parsed.typedefs:
                local.setdefault(name, []).append(variant)
    definitions = set()
    # A version-selected call can look like a signature after directives are
    # blanked. Read only file-scope signatures, using the same body scan as
    # declaration extraction, before deciding which contracts a source owns.
    for unit in source_definition_units(source_path, text, shared.project, shared.policy):
        for match in _SIGNATURE.finditer(unit):
            definitions.add(match["name"])
            prototype = match["prototype"].strip() + ";"
            variants = local.setdefault(match["name"], [])
            if prototype not in variants:
                variants.append(prototype)
    local_aliases = {**shared.typedefs, **shared.replacements, **redeclarations.aliases([text])}
    for name in local.keys() & shared.declarations_by_name.keys():
        if (name != source_path.stem or name in definitions) and any(
            not redeclarations.equivalent(variant, shared.declarations_by_name[name], local_aliases)
            for variant in local[name]
        ):
            drop_names.add(name)
    # Installed generated declarations are dependencies, not owners of a source-local or defined contract.
    # Conflicting local views stay local; inference already records their disagreement as declaration evidence.
    # A published declaration of a name this source defines yields to the definition when they disagree.
    for path in dict.fromkeys(path for name in local for path in shared.contracts_by_name.get(name, ())):
        if path in shared.components and any(
            not redeclarations.equivalent(variant, shared.components[path], local_aliases)
            for name in local.keys() & shared.retained_contracts[path]
            if path not in shared.published_homes or name in definitions
            for variant in local[name]
        ):
            drop_paths.add(path)
    return source_typedefs, drop_names, drop_paths


def unprototyped_calls(functions: dict[str, Any]) -> set[str]:
    """Functions whose mapped callers pass arguments the callee's (void) list cannot accept."""
    return {
        name
        for name, record in functions.items()
        if (record.get("abi") or {}).get("caller_arguments")
        and re.search(r"\(\s*void\s*\)\s*;\s*$", record["prototype"] or "")
    }


def void_calls(names: set[str]) -> re.Pattern[str] | None:
    """The pattern of every `name(void)` of NAMES (compiled once for a render), or None when there are no names."""
    if not names:
        return None
    return re.compile(r"\b(" + "|".join(sorted(map(re.escape, names))) + r")\s*\(\s*void\s*\)")


def without_void(text: str, pattern: re.Pattern[str] | None) -> str:
    """TEXT with each declaration the pattern names spelled `name()` instead of `name(void)`."""
    return text if pattern is None else pattern.sub(r"\1()", text)


def one_declaration(declarations_by_name: dict[str, str], carried: dict[Path, set[str]]) -> None:
    """One declaration per symbol: a published declaration that is still carried (it agrees with any
    definition, see the source loop above) replaces the solver's prototype for the same symbol, whatever
    either spells."""
    for names in carried.values():
        for name in names:
            declarations_by_name.pop(name, None)


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


def _consumer_imports(
    project: Project, outputs: dict[Path, bytes | Path], sources: dict[Path, str]
) -> dict[Path, bytes]:
    """Reconnect retained declarations moved by this publication, before installing headers.

    Old generated files may remain until the headers step, but an old home
    overwritten by this render no longer supplies its declarations. Project
    only affected imports; local declarations and function bytes stay intact.
    Missing declarations remain the loss guard's responsibility.
    """
    from unbake.fold import imports
    from unbake.layout import header_loss

    before = {path: path.read_text() for root in project.include for path in root.rglob("*.h")}
    after = dict(before)
    for path, data in outputs.items():
        if path.suffix == ".h":
            after[path] = (data.read_bytes() if isinstance(data, Path) else data).decode()
    if before == after:
        return {}
    parts = {text: header_loss._header(text) for text in set(before.values()) | set(after.values())}

    def view(contents: dict[Path, str]) -> header_loss.View:
        return header_loss.View(
            project.include,
            {path: parts[text][0] for path, text in contents.items()},
            {path: parts[text][2] for path, text in contents.items()},
        )

    old, new = view(before), view(after)
    kept = set().union(*(parts[text][0] for text in after.values()))
    dependencies: dict[str, set[str]] = {}
    for text in before.values():
        for name, dependency_words in parts[text][1].items():
            dependencies.setdefault(name, set()).update(dependency_words)
    changes = {}
    headers: Any = SimpleNamespace(texts=after)
    for source, text in sources.items():
        provided, words = header_loss._source(text)
        wanted = set(words)
        pending = list(wanted)
        while pending:
            added = dependencies.get(pending.pop(), set()) - wanted - provided
            wanted.update(added)
            pending.extend(added)
        unreachable = (old.included(source, text) - new.included(source, text)) & wanted & kept
        if unreachable:
            rewritten = imports.resolve(project, headers, text, source.stem)
            if rewritten != text:
                changes[source] = rewritten.encode()
    return changes


def publish(project: Project, value: dict[str, Any], previous: dict[str, Any], *, policy: Host | None = None) -> None:
    if not project.include:
        raise Held("solve", "paths.include: required shared type destination")
    with tui.task("Preparing header ownership"):
        session = regeneration.Session(project, policy)
    installed = {**session.authored, **session.installed}
    contracts = namespace.FunctionDeclarations(value, installed)
    session.function_declarations = contracts
    with tui.task("Writing the shared headers", len(session.sources)):
        outputs = session.render(value, lambda: _render(project, value, policy, session))
    from unbake.layout import header_loss, header_step

    canonical = namespace.publication_outputs(contracts, installed, session.sources, outputs)
    outputs.update(canonical)
    if canonical:
        from unbake.layout import index as layout_index

        listing = outputs[layout_index.path(project)]
        lookup = json.loads(listing.read_bytes() if isinstance(listing, Path) else listing)
        for path, data in canonical.items():
            if path in session.installed:
                lookup["headers"][path.relative_to(project.include[0]).as_posix()] = input_pins.bytes_digest(
                    data, algorithm="sha256"
                )
        outputs[layout_index.path(project)] = layout_index.encoded(lookup)
    sources = {path: canonical.get(path, text.encode()).decode() for path, text in session.sources.items()}
    reconnected = _consumer_imports(project, outputs, sources)
    outputs.update(reconnected)
    # Reconnect changed homes in the same installation as their consumers.
    # Obsolete, untouched homes remain until the regular headers step.
    with tui.task("Checking retained header declarations"):
        header_loss.check(project, outputs, policy=policy)
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
    if canonical and policy is None:
        raise Held("headers", "headers.namespace: canonical function declarations require native publication proof")
    if (reconnected or canonical) and policy is not None:
        native_outputs = {
            path: data.read_bytes() if isinstance(data, Path) else data
            for path, data in outputs.items()
            if path.suffix in (".h", ".c") and (not path.is_file() or path.read_bytes() != data)
        }
        header_step.validate(project, policy, native_outputs, prove_all=bool(canonical))
    with tui.task("Saving the type database"):
        value["rendered_sha256"] = {
            storage.relative(project, path): input_pins.bytes_digest(content, algorithm="sha256")
            for path, content in outputs.items()
            if isinstance(content, bytes)
        }
        database = types_db.path(project)
        encoded = types_db.encode(value)
        digest = types_db.content_digest(encoded)
        summary: dict[str, Any] = {}
        changed: set[str] = set()
        for kind in ("functions", "globals", "structs", "arrays"):
            before = previous.get(kind, {})
            after = value[kind]

            summary[kind] = {
                name: {
                    "semantic_sha256": input_pins.bytes_digest(storage.encoded(_semantic(row)), algorithm="sha256"),
                    "users": list(row.get("users", [])),
                }
                for name, row in after.items()
            }
            for name in set(before) | set(after):
                old = before.get(name, {})
                old_digest = old.get("semantic_sha256") or input_pins.bytes_digest(
                    storage.encoded(_semantic(old)), algorithm="sha256"
                )
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
        staged, _ = types_db.stage(database, encoded, summary, marks)
        from unbake.layout import index

        # Sources still include headers this render no longer produces until the headers step rewrites them, and
        # that step deletes them in the same install; until then they stay listed as generated.
        retained = index.headers(project) - outputs.keys()
        if retained:
            listing = outputs[index.path(project)]
            lookup = json.loads(listing.read_bytes() if isinstance(listing, Path) else listing)
            for path in retained:
                if path.is_file():
                    lookup["headers"][path.relative_to(project.include[0]).as_posix()] = input_pins.digest(
                        path, algorithm="sha256", reuse=retention.configured()
                    )
            outputs[index.path(project)] = index.encoded(lookup)
        outputs = {
            path: content
            for path, content in outputs.items()
            if not path.is_file()
            or (
                input_pins.digest(path, algorithm="sha256", reuse=retention.configured())
                != input_pins.digest(content, algorithm="sha256", reuse=retention.configured())
                if isinstance(content, Path)
                else path.read_bytes() != content
            )
        }
        backups: dict[Path, Path | None] = {}
        try:
            for path in set(outputs) | {database}:
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
    from unbake.cache import Cache, key, memo

    def remembered_digest(data: bytes) -> str:
        return memo(
            "typemap-validation-digest",
            data,
            lambda: input_pins.bytes_digest(data, algorithm="sha256"),
            size=retention.memory_size,
            copy_out=retention.clone,
        )

    cache = Cache(project.cache)
    environment = session.environment if session is not None else regeneration.environment(project, policy)
    is_generated = storage.generated_view(project)
    authored = (
        session.authored
        if session is not None
        else {
            path: path.read_text() for root in project.include for path in root.rglob("*.h") if not is_generated(path)
        }
    )
    bundle_key = key(
        environment,
        abi_context,
        *(part for path, text in authored.items() for part in (storage.relative(project, path), text)),
        *(
            part
            for path, content in outputs.items()
            if isinstance(content, bytes)
            for part in (storage.relative(project, path), content)
        ),
    )
    bundle = cache.get("typemap-validation", bundle_key)
    if bundle is not None:
        total = json.loads(bundle.read_bytes())["rows"]
        if type(total) is not int or total < 0:
            raise Held("solve", "types.validation.rows: expected nonnegative integer")
        effort.count("validation.rows", 0, total)
        effort.count("validation.bundle_hit", 1, 1)
        return
    effort.count("validation.bundle_hit", 0, 1)
    try:
        contents, closures, abi = regeneration.validation_inputs(project, outputs, abi_context, authored=authored)
    except Held as error:
        raise Held("solve", f"types.header_parse: {error.reason}") from error
    digests = {path: remembered_digest(data) for path, data in contents.items()}
    certificates = (
        session.certificates if session is not None else cache.certificates("typemap-certificates", environment)
    )
    if validated is None:
        validated = set()
    rows: list[tuple[str, set[Path], str]] = []

    def signature(name: str, inputs: set[Path]) -> str:
        pinned = sorted((storage.relative(project, dep), digests[dep]) for dep in inputs)
        return memo(
            "typemap-validation-signature",
            (environment, name, tuple(pinned)),
            lambda: key(environment, name, *(part for pair in pinned for part in pair)),
            size=retention.memory_size,
            copy_out=retention.clone,
        )

    for path, closure in closures.items():
        if path not in outputs:
            continue
        # Include-only indexes observe authored prerequisites; generated leaves
        # each carry their own certificates.
        body = re.sub(r"^[ \t]*#[^\n]*|/\*.*?\*/|//[^\n]*", "", contents[path].decode(), flags=re.M | re.S)
        inputs = closure if body.strip() else {path, *(dep for dep in closure if dep not in outputs)}
        rows.append((signature(storage.relative(project, path), inputs), closure, ""))
    rows.extend((signature(text, closure), closure, text) for text, closure in abi)
    from unbake import pool

    pending_by_version: dict[str, dict[str, tuple[set[Path], str]]] = {}
    for version in project.versions:
        pending: dict[str, tuple[set[Path], str]] = {}
        for input_key, closure, text in rows:
            content_key = version + ":" + input_key
            if content_key not in validated and not certificates.contains((content_key,)):
                pending[content_key] = closure, text
        effort.count("validation.rows", len(pending), len(rows))
        if pending:
            pending_by_version[version] = pending
    # Total jobs ~ workers: a version with more pending rows than one worker's share is split into chunks.
    workers = pool.workers(policy) if policy is not None else 1
    jobs: list[_Validation] = []
    for version, pending in pending_by_version.items():
        names = list(pending)
        width = pool.width(len(names) * len(pending_by_version), workers, 1)
        for start in range(0, len(names), width):
            part = {name: pending[name] for name in names[start : start + width]}
            selected = {path for closure, _ in part.values() for path in closure}
            generated = selected.intersection(outputs)
            jobs.append(
                _Validation(
                    project,
                    policy,
                    version,
                    {path: contents[path] for path in selected},
                    {path: closures[path] for path in generated},
                    list(dict.fromkeys(text for _, text in part.values() if text)),
                    frozenset(part),
                )
            )
    # Each chunk's staged context is preprocessed and parsed on its own (cpp, m2c): one pool task per chunk.
    # The parent records the certificates in job order; the first refused chunk is reported.
    project.build.mkdir(parents=True, exist_ok=True)
    with tui.task("Test-compiling the shared headers", len(jobs)):
        parsed = (
            pool.run(policy, _validate_version, jobs) if policy is not None else [_validate_version(j) for j in jobs]
        )
    for job, context_key in zip(jobs, parsed, strict=True):
        validated.add(context_key)
        certificates.add(set(job.pending))
        validated.update(job.pending)

    def complete(path: Path) -> None:
        atomic_files.fresh(path, storage.encoded({"rows": len(rows) * len(project.versions)}))

    cache.produce("typemap-validation", bundle_key, complete)


@dataclass(frozen=True)
class _Validation:
    """One chunk of a version's pending validation: the staged header bytes it reads and the ABI texts it adds."""

    project: Project
    policy: Host | None
    version: str
    contents: dict[Path, bytes]
    closures: dict[Path, set[Path]]
    texts: list[str]
    pending: frozenset[str]


def _validate_version(job: _Validation) -> str:
    """Stage the selected headers, preprocess them for the version and parse the context (a pool task).
    Returns the parsed context's key."""
    from dataclasses import replace

    from unbake.cache import key
    from unbake.decomp.draft_context import ordered_headers, preprocess_context
    from unbake.process import run_tool
    from unbake.typemap import declarations

    project, policy, version, contents = job.project, job.policy, job.version, job.contents
    selected = set(contents)
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
                    atomic_files.write(staged, contents[path], durable=False)
        staged_project = replace(project, work_include=tuple(roots))
        source = scratch / "context.c"
        generated = set(job.closures)
        dependencies = {dep for path in generated for dep in job.closures[path] if dep != path}
        entry_points = sorted(generated - dependencies)
        if not entry_points:
            entry_points = sorted(generated)
        covered = {dep for path in entry_points for dep in job.closures[path]}
        entry_points.extend(sorted(selected - covered))
        # Native cpp follows include order verbatim. Order the entry points by
        # the complete declarations supplied by their include closures, just
        # as selected draft contexts do, before preprocessing the real tree.
        entry_points = ordered_headers({path: contents[path].decode() for path in sorted(selected)}, roots=entry_points)
        atomic_files.text(
            source,
            durable=False,
            content="".join(
                f'#include "{str(path)[len(str(root)) + 1 :]}"\n'
                for path in entry_points
                for root in project.include
                if str(path).startswith(str(root) + os.sep)
            ),
        )
        assembly = scratch / "validate.s"
        atomic_files.text(assembly, ".text\nglabel __unbake_validate_context\n jr $ra\n nop\n", durable=False)
        context_text = source.read_text()
        try:
            if policy is None:
                ordered = ordered_headers({path: contents[path].decode() for path in sorted(selected)})
                expanded = "\n".join(declarations.clean(contents[path].decode()) for path in ordered)
            else:
                expanded = preprocess_context(source, staged_project, policy, version, "__unbake_validate_context")
            context_text = expanded + "\n" + "\n".join(job.texts)
            if policy is not None and getattr(policy, "m2c", None):
                context = scratch / "expanded.c"
                atomic_files.text(context, context_text, durable=False)
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
        except Held as error:
            context = project.build / "types" / f"held-header-context-{version}-{key(context_text)[:12]}.c"
            context.parent.mkdir(parents=True, exist_ok=True)
            atomic_files.text(context, context_text, durable=False)
            headers = ", ".join(map(str, entry_points))
            raise Held(
                "solve", f"types.header_parse: {version}: headers {headers}; context {context}: {error.reason}"
            ) from error
    return key(context_text)
