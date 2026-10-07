"""Select authored declaration evidence and feed the normal shared type path."""

from __future__ import annotations

import base64
import hashlib
import os
import re
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any

from unbake import cache as retention
from unbake import inputs, pool, tui
from unbake.cache import Cache, key, memo
from unbake.cdecl import LayoutParser, declaration_source, declarations
from unbake.config import Held, Host, Project, relative_text
from unbake.decomp.draft_context import ordered_headers
from unbake.fold import imports
from unbake.layout import split as inventory
from unbake.layout.header_context import Headers
from unbake.layout.split import Edit
from unbake.project.headers import Graph, ProviderSet, scan
from unbake.typemap import split, storage

_MARKER = re.compile(r"/\* unbake declaration evidence: (evidence_[a-f0-9]+) \*/")
_DEFINE = re.compile(r"^[ \t]*#[ \t]*define[ \t]+([A-Za-z_]\w*)(?:\\\n|[^\n])*", re.M)


@dataclass(frozen=True)
class Unit:
    path: Path
    text: str
    names: frozenset[str]
    types: frozenset[str]
    tags: frozenset[str]
    uses: frozenset[str]


def files(project: Project) -> tuple[Path, ...]:
    roots = getattr(project, "declaration_evidence", ())
    for root in roots:
        if not root.is_dir():
            raise Held("types", f"declaration_evidence: include tree missing: {root}")
    return tuple(sorted({path for root in roots for path in root.rglob("*.h") if path.name != "m2c_prelude.h"}))


def _body(text: str) -> str:
    """Remove one enclosing include guard, retaining semantic conditionals."""
    guard = re.match(r"\s*#\s*ifndef\s+(\w+)\s*\n\s*#\s*define\s+\1\s*\n", text)
    end = re.search(r"^\s*#\s*endif[^\n]*\s*\Z", text, re.M)
    if guard and end:
        text = text[guard.end() : end.start()]
    return text


def units(contents: dict[Path, str]) -> tuple[Unit, ...]:
    return tuple(
        unit
        for path, text in sorted(contents.items())
        for unit in memo(
            "declaration.units",
            (path, text),
            partial(_units, path, text),
            size=retention.memory_size,
            copy_out=retention.clone,
        )
    )


def _units(path: Path, text: str) -> tuple[Unit, ...]:
    result = []
    # SDK/system declarations already have live providers. Evidence is an
    # explicit include tree, but only authored declarations are consumed.
    try:
        projection = Graph.contents({path: text}, (path.parent,)).projection(path)
        if projection.parse_error:
            raise Held("headers", projection.parse_error)
        statements = projection.statements
    except Held:
        return ()
    for statement in statements:
        if re.match(r"\s*#\s*(?:include|pragma|undef)\b", statement):
            continue
        try:
            row = declarations(statement)
        except Held:
            continue
        macros = {m[1] for m in _DEFINE.finditer(statement)}
        constants = {
            match[1]
            for body in re.findall(r"\benum\b[^{};]*\{([^{}]*)\}", declaration_source(statement))
            for member in body.split(",")
            if (match := re.match(r"\s*([A-Za-z_]\w*)", member))
        }
        names = row.typedefs | row.declared | constants | macros
        if any(name.startswith("M2C_") for name in names):
            continue
        if not names and not row.tags:
            continue
        # Definitions/initializers are implementation, never declaration evidence.
        if re.search(r"\)\s*\{", declaration_source(statement)) or (
            row.declared and re.search(r"=|\bstatic\b", declaration_source(statement))
        ):
            continue
        uses = row.uses | row.complete_uses
        # Pointer tag references are not complete/by-value uses in the name
        # parser, but retained consumers can dereference them. Follow their
        # installed definitions and same-named canonical typedefs as well.
        uses.update(re.findall(r"\b(?:struct|union|enum)\s+(\w+)", declaration_source(statement)))
        # Array extents and conditional tests are declaration dependencies
        # too, although the declarator reader deliberately skips expressions.
        for expression in re.findall(r"\[([^]]*)\]|^[ \t]*#[ \t]*(?:if|elif|ifdef|ifndef)\b([^\n]*)", statement, re.M):
            uses.update(re.findall(r"\b[A-Za-z_]\w*\b", " ".join(expression)))
        if macros:
            uses |= set(re.findall(r"\b[A-Za-z_]\w*\b", statement)) - macros
        result.append(
            Unit(
                path,
                statement.strip() + "\n",
                frozenset(names),
                frozenset(row.typedefs),
                frozenset(row.tags),
                frozenset(uses),
            )
        )
    return tuple(result)


def catalogue(project: Project) -> tuple[Unit, ...]:
    paths = files(project)
    contents = {path: path.read_text() for path in paths}
    return memo(
        "declaration.evidence",
        tuple(sorted(contents.items())),
        lambda: units(contents),
        size=retention.memory_size,
        copy_out=retention.clone,
    )


def select(project: Project, headers: Headers, text: str, function: str) -> tuple[Unit, ...]:
    """Find needed missing declarations, retaining ambiguity as an explicit hold."""
    rows = catalogue(project)
    if not rows:
        return ()
    live = memo(
        "imports.providers",
        tuple(sorted(headers.texts.items())),
        lambda: ProviderSet(Graph.contents(headers.texts, project.include)),
        size=retention.memory_size,
        copy_out=retention.clone,
    )
    graph = Graph.contents(headers.texts, project.include)
    source = graph.rewrite_imports(imports._without_comments(text), lambda include, original: "")
    local = declarations(source)
    blocked = local.typedefs | local.declared | {function} | {m[1] for m in _DEFINE.finditer(source)}
    words = set(re.findall(r"\b[A-Za-z_]\w*\b", re.sub(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'', "", source)))
    by_name: dict[str, list[Unit]] = {}
    for unit in rows:
        for name in unit.names | unit.tags:
            by_name.setdefault(name, []).append(unit)
    requested = {include.name for include in scan(text)}
    preferred = {
        unit.path
        for unit in rows
        if any(
            unit.path.is_relative_to(root) and unit.path.relative_to(root).as_posix() in requested
            for root in getattr(project, "declaration_evidence", ())
        )
        and not any(
            root / unit.path.relative_to(old) in headers.texts
            for old in getattr(project, "declaration_evidence", ())
            if unit.path.is_relative_to(old)
            for root in project.include
        )
    }
    explicit_types = set().union(*(unit.types | unit.tags for unit in rows if unit.path in preferred))
    pending = (words - live.names.keys() - live.tags.keys() | words & explicit_types) - blocked
    selected: dict[str, Unit] = {}
    seen = set()
    while pending:
        name = min(pending)
        pending.remove(name)
        if name in seen:
            continue
        seen.add(name)
        options = by_name.get(name, [])
        if not options:
            continue
        # A forward declaration is not a competing full layout provider.
        preferred_options = [u for u in options if u.path in preferred]
        if preferred_options:
            options = preferred_options
        complete = [u for u in options if "{" in declaration_source(u.text)]
        if complete:
            options = complete
        spellings = {re.sub(r"\s+", " ", u.text).strip() for u in options}
        if len(spellings) != 1:
            raise Held(
                "types",
                f"declaration_evidence: {name}: ambiguous authored declarations: "
                + ", ".join(str(u.path) for u in options),
            )
        unit = options[0]
        selected[unit.text] = unit
        # C's tag and typedef namespaces are distinct. A complete `struct T`
        # does not supply the authored `typedef struct T T` used by consumers.
        # Keep that compatible declaration alongside the chosen full layout.
        if name in unit.tags and name not in unit.types:
            for alias in by_name.get(name, []):
                if name in alias.types and re.fullmatch(
                    rf"typedef\s+(struct|union)\s+{re.escape(name)}\s+{re.escape(name)}\s*;\s*", alias.text
                ):
                    selected[alias.text] = alias
        pending.update(unit.uses - blocked - live.names.keys() - live.tags.keys() - seen)
    return tuple(selected.values())


def validate_symbols(project: Project, selected: tuple[Unit, ...], versions: tuple[str, ...]) -> None:
    """Accept extern evidence only for names in the live address inventory."""
    symbols = {
        version: inventory.symbols(project.version(version).symbols)[1]
        for version in getattr(project, "versions", versions)
    }
    for unit in selected:
        row = declarations(unit.text)
        macro_addresses = {}
        for macro in _DEFINE.finditer(unit.text):
            if re.search(r"\([^)]*\*[^)]*\)", macro[0]):
                addresses = re.findall(r"\b0x[89aAbB][0-9A-Fa-f]{7}\b", macro[0])
                if addresses:
                    macro_addresses[macro[1]] = {int(value, 16) for value in addresses}
        for name in row.declared | macro_addresses.keys():
            found = {version: values[name][0] for version, values in symbols.items() if name in values}
            if not found:
                raise Held("types", f"declaration_evidence: {name}: absent live symbol/address inventory ({unit.path})")
            address = re.fullmatch(r"D_([0-9A-Fa-f]{8})(?:_\w+)?", name)
            if address and int(address[1], 16) not in found.values():
                raise Held("types", f"declaration_evidence: {name}: live address disagrees with declaration identity")
            if name in macro_addresses and not macro_addresses[name] <= set(found.values()):
                raise Held("types", f"declaration_evidence: {name}: address-valued macro disagrees with live inventory")


def inject(project: Project, headers: Headers, text: str, function: str, versions: tuple[str, ...]) -> tuple[str, int]:
    selected = select(project, headers, text, function)
    if not selected:
        return text, 0
    validate_symbols(project, selected, versions)
    defines = dict(headers.defines)
    for unit in selected:
        defines.update(LayoutParser(unit.text).defines)
    constants = LayoutParser("")
    constants.defines = defines
    contents = {}
    for i, unit in enumerate(selected):
        declaration = unit.text
        anonymous = re.match(r"typedef\s+(struct|union)\s*\{", declaration)
        alias = re.search(r"}\s*([A-Za-z_]\w*)\s*;\s*$", declaration)
        if anonymous and alias:
            # Give the authored anonymous layout its existing typedef identity.
            # The collision resolver needs a tag before it can rewrite aliases.
            declaration = declaration[: anonymous.end() - 1] + alias[1] + " {" + declaration[anonymous.end() :]
        if not _DEFINE.search(declaration):

            def extent(match: re.Match[str]) -> str:
                if not match[1].strip():
                    return match[0]
                try:
                    return "[" + str(constants.expression(match[1])) + "]"
                except Held:
                    return match[0]

            declaration = re.sub(r"\[([^]\n]*)\]", extent, declaration)
        contents[Path(f"/evidence/{i}.h")] = declaration
    aggregate_aliases = {}
    for declaration in contents.values():
        definition = re.match(r"typedef\s+((?:struct|union)\s+\w+)\s*\{", declaration)
        if definition:
            for name in declarations(declaration).typedefs:
                aggregate_aliases[name] = definition[1]
    changed = True
    while changed:
        changed = False
        for path, declaration in contents.items():
            alias = re.fullmatch(r"typedef\s+(\w+)\s+(\w+)\s*;\s*", declaration)
            if alias and alias[1] in aggregate_aliases:
                aggregate_aliases[alias[2]] = aggregate_aliases[alias[1]]
                contents[path] = f"typedef {aggregate_aliases[alias[1]]} {alias[2]};\n"
                changed = True
    prefix = "".join(contents[path] for path in ordered_headers(contents))
    return prefix + text, len(prefix)


def promote(project: Project, headers: Headers, final: str, prefix_end: int) -> tuple[str, list[Edit]]:
    """Move remaining evidence aliases, prototypes and macros into generated components."""
    if not prefix_end:
        return final, []
    # The caller puts a sentinel after injected evidence; aggregate folding can
    # change its length. Everything before it still belongs to the evidence.
    prefix, marker, source = final.partition("/* unbake declaration evidence boundary */\n")
    if not marker:
        raise Held("types", "declaration_evidence: lost source boundary during layout fold")
    components = {}
    for statement in split.statements(_body(prefix)):
        if re.match(r"\s*#\s*include\b", statement):
            source = statement + "\n" + source
            continue
        if not statement.strip():
            continue
        label = "evidence_" + hashlib.sha256(statement.encode()).hexdigest()[:24]
        virtual = project.include[0] / "shared" / ("." + label + ".h")
        body = f"/* unbake declaration evidence: {label} */\n" + statement
        components[virtual] = imports.resolve(project, headers, body)
    from unbake.layout import headers as mapped_headers
    from unbake.layout import index, map

    sources = {path: path.read_text() for path in project.src.rglob("*.c")}
    function = next(
        (name for name in map.load(project).owners if re.search(r"\b" + re.escape(name) + r"\s*\(", source)), None
    )
    if function is not None:
        sources[project.src / (function + ".c")] = source
    ownership = map.load(project)
    known = set(index.headers(project)) | {project.include[0] / group.header for group in ownership.groups}
    existing = {path: _body(headers.texts[path]) for path in known if path in headers.texts}
    combined = {**existing, **components}
    layout = mapped_headers.Layout(combined, combined, project.include[0], ownership=ownership, sources=sources)
    edits = [
        Edit(path, headers.texts.get(path, ""), body.decode(), tuple(project.versions))
        for path, body in layout.headers.items()
        if headers.texts.get(path, "") != body.decode()
    ]
    source = imports.resolve(project, headers, source, edits=tuple(edits))
    return source, edits


def published_components(project: Project) -> dict[Path, str]:
    return published_snapshot(project)[0]


@pool.cpu
def _compatible(shared: Any, job: Any) -> bool:
    mapping, cache_root = shared
    content_key, unit, candidate, tags = job
    from unbake.layout import redeclarations

    if tags:
        old_bodies = redeclarations.tag_definitions(unit.text)
        new_bodies = redeclarations.tag_definitions(candidate)
        agrees = all(
            redeclarations._body_signature(unit.text[a:b], mapping)
            == redeclarations._body_signature(candidate[c:d], mapping)
            for tag in tags
            for a, b in old_bodies.get(tag, ())
            for c, d in new_bodies.get(tag, ())
        )
    else:
        agrees = redeclarations.equivalent(unit.text, candidate, mapping)
    if agrees and cache_root is not None:

        def write(target: Path) -> None:
            target.write_bytes(b"compatible")

        Cache(cache_root).produce("types-contract", content_key, write)
    return agrees


def validate_published(
    project: Project,
    value: dict[str, Any],
    published: dict[str, str],
    *,
    context: tuple[str, ...] = (),
    policy: Host | None = None,
) -> None:
    """Carried contracts yield only to compatible proven evidence, never silently.

    Machine inference can lose a spelling or offer a weaker alternative. Only
    a proved C contract contradicting an installed consumer contract is a hold.
    """
    from unbake.layout import redeclarations

    def proven(provenance: object) -> bool:
        if isinstance(provenance, dict):
            return provenance.get("kind") == "proven"
        return isinstance(provenance, list) and any(proven(item) for item in provenance)

    contents = {project.include[0] / path: text for path, text in published.items()}
    contracts = units(contents)
    by_name: dict[tuple[str, str], set[Unit]] = {}
    for unit in contracts:
        for namespace, names in (("tag", unit.tags), ("ordinary", unit.names)):
            for name in names:
                by_name.setdefault((namespace, name), set()).add(unit)
    mapping = redeclarations.aliases([*context, *contents.values()])
    environment = key(
        "types-contract-v1",
        storage.encoded(mapping),
        inputs.digest(Path(__file__), algorithm="sha256", reuse=retention.configured()),
        inputs.digest(Path(redeclarations.__file__), algorithm="sha256", reuse=retention.configured()),
    )
    cache = None if policy is None else Cache(project.cache)
    jobs = []
    owners = []
    for kind, field in (("structs", "declaration"), ("functions", "prototype"), ("globals", "declaration")):
        for name, record in value.get(kind, {}).items():
            candidate = record.get(field)
            if record.get("state") != "known" or not candidate or not proven(record.get("provenance")):
                continue
            proposed = declarations(candidate)
            matches = set().union(
                *(by_name.get(("tag", tag), set()) for tag in proposed.tags),
                *(by_name.get(("ordinary", symbol), set()) for symbol in proposed.typedefs | proposed.declared),
            )
            for unit in sorted(matches, key=lambda unit: (unit.path, unit.text)):
                tags = proposed.tags & unit.tags
                ordinary = (proposed.typedefs | proposed.declared) & unit.names
                if not tags and not ordinary:
                    continue
                content_key = key(environment, unit.text, candidate, storage.encoded(sorted(tags)))
                if cache is not None and cache.get("types-contract", content_key) is not None:
                    continue
                jobs.append((content_key, unit, candidate, tags))
                owners.append((name, record))
    with tui.task("Checking changed published contracts", len(jobs)):
        shared = mapping, None if cache is None else cache.root
        results = (
            [_compatible(shared, job) for job in jobs]
            if policy is None
            else pool.run(policy, _compatible, jobs, shared)
        )
    for (_, unit, candidate, _), (name, record), agrees in zip(jobs, owners, results, strict=True):
        if not agrees:
            homes = value.get("published_homes", {}).get(unit.path.relative_to(project.include[0]).as_posix(), [])
            raise Held(
                "headers",
                f"headers.declaration: {name}: published `{unit.text.strip()}` "
                f"({', '.join(homes) or str(unit.path)}) is incompatible with proven "
                f"`{candidate}` ({record.get('provenance')})",
            )


def published_snapshot(
    project: Project, *, sources: dict[Path, str] | None = None, contents: dict[Path, str] | None = None
) -> tuple[dict[Path, str], dict[Path, set[Path]]]:
    """Keep installed declaration dependencies used by published C bodies.

    Generated does not mean disposable: a matched body proves the declaration
    spellings it consumes, including callback arrays and complete queue layouts.
    Follow declaration dependencies to keep primitive aliases and member types.
    """
    from unbake.layout import apply, index
    from unbake.typemap.declarations import published_sources

    if contents is None:
        contents = {path: path.read_text() for path in sorted(index.headers(project))}
    rows = units(contents)
    by_name: dict[str, list[Unit]] = {}
    for unit in rows:
        for name in unit.names | unit.tags:
            by_name.setdefault(name, []).append(unit)
    from unbake.layout import redeclarations

    if sources is None:
        sources = {source: source.read_text() for _, source, _ in published_sources(project)}
    selected: dict[Path, str] = {}
    homes: dict[Path, set[Path]] = {}
    for text in sources.values():
        # Ownership is local to one consumer. A private same-named tag in a
        # different unit cannot erase this unit's shared declaration dependency.
        local_tags = redeclarations.local_tags(text)
        local_types = set().union(
            *(
                declarations(variant).typedefs
                for start, end in redeclarations.spans(text)
                for variant in redeclarations.variants(text[start:end])
            )
        )
        # Directive blanking would hide native uses inside source macros.
        # Use the same roots as import rewriting and provider selection.
        pending = {(name, False) for name in apply.spelled(text)}
        seen = set()
        while pending:
            name, dependency = pending.pop()
            if (name, dependency) in seen:
                continue
            seen.add((name, dependency))
            for unit in by_name.get(name, ()):
                if not dependency and (unit.tags & local_tags or unit.types & local_types):
                    continue
                digest = hashlib.sha256(unit.text.encode()).hexdigest()[:24]
                label = "published_" + digest
                virtual = project.include[0] / ("." + label + ".h")
                homes.setdefault(virtual, set()).add(unit.path)
                selected[virtual] = f"/* unbake published declaration: {label} */\n" + unit.text
                # Prerequisites need providers in header scope even if this
                # consumer also defines a private same-named type.
                pending.update((used, True) for used in unit.uses)
    return selected, homes


def feedback_components(project: Project) -> dict[Path, str]:
    """Retain ingested evidence through solve/feedback's ordinary split generator."""
    from unbake.layout import index

    result = {}
    root = project.include[0]
    for path in sorted(index.headers(project)):
        text = path.read_text()
        body = re.sub(r"^\s*#\s*include[^\n]*\n?", "", _body(text), flags=re.M)
        markers = list(_MARKER.finditer(body))
        for position, marker in enumerate(markers):
            stop = markers[position + 1].start() if position + 1 < len(markers) else len(body)
            virtual = root / ("." + marker[1] + ".h")
            unit = body[marker.start() : stop].strip() + "\n"
            retained = re.search(r"/\* unbake evidence input: ([A-Za-z0-9+/=]+) \*/", unit)
            if retained:
                try:
                    unit = base64.b64decode(retained[1], validate=True).decode()
                except (ValueError, UnicodeDecodeError) as error:
                    raise Held("types", f"declaration_evidence: {path}: malformed retained input") from error
            result[virtual] = unit
    return result


def plan_many(project: Project, policy: Host, sources: tuple[Path, ...]) -> tuple[list[Edit], list[dict[str, str]]]:
    """Admit authored declarations independently of a function's instruction proof.

    This is declared evidence, not a matched-function receipt. Planning uses the
    normal collision/layout fold; solve validates the resulting shared context.
    """

    from unbake.fold import declarations as source_declarations

    headers = Headers.read(project)
    original = dict(headers.texts)
    reports = []
    for source in sources:
        try:
            text = source.read_text()
            prefix, end = inject(project, headers, text, source.stem, tuple(project.versions))
            if not end:
                reports.append(
                    {
                        "source": os.path.relpath(source, project.root),
                        "status": "reused",
                        "reason": "no absent authored declarations",
                    }
                )
                continue
            prefix = prefix[:end]
            parser, records = headers.parse(prefix)
            wanted = declarations(prefix).typedefs - headers.types.keys()
            synthetic = prefix + "/* unbake declaration evidence boundary */\n" + f"void {source.stem}(void) {{}}\n"
            folded = source_declarations.fold_source(
                project,
                policy,
                headers,
                source.stem,
                synthetic,
                tuple(project.versions),
            )
            context = Headers(
                {**headers.texts, **{edit.path: edit.after for edit in folded.headers}}, root=project.root
            )
            resolution = context.index.resolve(records, source.stem)
            aliases = []
            for name in sorted(wanted - context.types.keys()):
                base = parser.types.get(name)
                spelling = parser.type_name(*base) if isinstance(base, tuple) else ""
                tag = re.fullmatch(r"(?:struct|union) (\w+)", spelling)
                if tag and tag[1] in resolution:
                    target, layout = resolution[tag[1]]
                    aliases.append(f"typedef {layout.kind} {target} {name};\n")
            final = folded.source
            if aliases:
                final = "".join(aliases) + final
            _, supplemental = promote(project, context, final, end)
            headers.apply([*folded.headers, *supplemental])
            # Aggregate folding consumes the injected prefix. Retain its
            # canonical declarations as declared evidence for the solve that
            # follows, independently of any instruction proof.
            for edit in [*folded.headers, *supplemental]:
                body = _body(headers.texts[edit.path])
                if not body.strip() or _MARKER.search(body):
                    continue
                label = "evidence_" + hashlib.sha256(body.encode()).hexdigest()[:24]
                retained = base64.b64encode(body.encode()).decode()
                text = headers.texts[edit.path]
                end = text.rfind("#endif")
                marker = f"/* unbake declaration evidence: {label} */\n/* unbake evidence input: {retained} */\n"
                headers.texts[edit.path] = text[:end] + marker + text[end:]
            reports.append(
                {
                    "source": os.path.relpath(source, project.root),
                    "status": "admitted",
                    "reason": "authored declarations through shared fold",
                }
            )
        except (Held, OSError) as error:
            reports.append(
                {
                    "source": os.path.relpath(source, project.root),
                    "status": "held",
                    "reason": relative_text(project.root, str(error)),
                }
            )
    edits = [
        Edit(path, original.get(path, ""), text, tuple(project.versions))
        for path, text in headers.texts.items()
        if original.get(path, "") != text
    ]
    return edits, reports
