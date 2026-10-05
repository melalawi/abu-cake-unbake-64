"""Preflight shared layouts and prepare final source declarations."""

from __future__ import annotations

import re
import tempfile
from collections.abc import Iterable
from contextlib import ExitStack
from dataclasses import dataclass, replace
from functools import partial
from pathlib import Path

from unbake.cdecl import LayoutParser
from unbake.config import Held, Host, Project
from unbake.decomp import gbi_recover, needs
from unbake.fold import imports, pool_literals, rewrite_view, source_views, type_rewrite
from unbake.fold import notes as reporting
from unbake.fold.common import held
from unbake.layout import entries, shared, split, structs
from unbake.layout.header_context import Headers, header_guard
from unbake.layout.split import Edit
from unbake.layout.structs_fold import _scalar_include, fold, scalar_edits
from unbake.layout.structs_types import Aggregate
from unbake.typemap.header_names import alias_types, callback_renames, type_identity


def preflight(project: Project, policy: Host, pending: list[needs.Need]) -> list[Edit]:
    """Validate shared layout needs without writes, copies, generations, or builds.

    Call after deriving trial needs and before retaining or submitting a trial.
    Missing aggregates produce proposed shared-header edits; conflicts raise Held.
    """
    return structs.resolve([need for need in pending if isinstance(need, needs.LayoutNeed)], project, policy)


def final_source(
    project: Project, text: str, parsers: list[LayoutParser], edits: list[Edit], headers: Headers, destination: Path
) -> str:
    """Move local aggregate definitions to their shared homes and remove draft markers."""
    records = [record for parser in parsers for record in _records(parser)]
    names = {name for record in records for name in (record.name, *record.aliases)}
    includes: set[str] = set()
    replacements: list[tuple[int, int, str]] = []
    for parser in parsers:
        selected, removed = scalar_edits(project, parser, headers)
        includes.update(selected)
        replacements.extend(removed)
    # A forward alias has no local layout, so it is absent from records.
    # Folding another layout can import its shared home and expose a duplicate
    # typedef that older compilers reject. Retire compatible shared aliases as
    # well, preserving distinct aliases and pointer/array declarators.
    shared_parser = headers.parser()
    shared_aliases = {name: type_ for value in headers.texts.values() for name, type_ in alias_types(value).items()}
    for parser in parsers:
        local_aliases = dict(shared_aliases)
        for start, end in sorted({(item.start, item.end) for item in parser.declarations}):
            local_aliases.update(alias_types(parser.source[start:end]))
        for start, end in sorted({(item.start, item.end) for item in parser.declarations}):
            local = LayoutParser(parser.source[start:end])
            if local.peek() != "typedef":
                continue
            local.take()
            members = local.declaration(typedef=True)
            if any(
                isinstance(member.base, Aggregate) and (member.base.complete or member.base.name in names)
                for member in members
            ):
                continue
            retained = []
            for member in members:
                target = headers.types.get(member.name)
                aggregate_match = (
                    isinstance(member.base, Aggregate)
                    and isinstance(target, tuple)
                    and isinstance(target[0], Aggregate)
                    and local.type_name(member.base, member.operations) == shared_parser.type_name(*target)
                    and not re.search(r"\b(?:const|volatile|restrict|__restrict)\b", member.declaration)
                )
                callback_match = False
                if (
                    member.name in shared_aliases
                    and member.name in local_aliases
                    and any(kind == "function" for kind, _ in member.operations)
                ):
                    callback_match = type_identity(local_aliases[member.name], local_aliases) == type_identity(
                        shared_aliases[member.name], shared_aliases
                    )
                if member.name in headers.homes and (aggregate_match or callback_match):
                    alias_home = headers.homes[member.name]
                    includes.add(
                        next(
                            alias_home.relative_to(root).as_posix()
                            for root in project.include
                            if alias_home.is_relative_to(root)
                        )
                    )
                else:
                    retained.append(member)
            if len(retained) != len(members):
                replacements.append((start, end, "\n".join("typedef " + member.declaration for member in retained)))
    if records:
        added = {edit.path for edit in edits}
        spans: list[tuple[int, int]] = []
        for record in records:
            home = headers.homes.get(f"{record.kind} {record.name}") or headers.homes.get(record.name)
            home = home or (destination if destination in added else None)
            if home is None:
                held(f"{record.name}: shared declaration home missing after fold")
            providers = {home, *(headers.homes[name] for name in record.aliases if name in headers.homes)}
            for provider in providers:
                includes.add(
                    next(
                        provider.relative_to(root).as_posix()
                        for root in project.include
                        if provider.is_relative_to(root)
                    )
                )
        for declaration in (item for parser in parsers for item in parser.declarations):
            base = declaration.base
            name = base if isinstance(base, str) else base.name
            if name in names and not declaration.operations:
                spans.append((declaration.start, declaration.end))
        replacements.extend((start, end, "") for start, end in set(spans))
    for start, end, replacement in sorted(set(replacements), reverse=True):
        text = text[:start] + replacement + text[end:]
    for include in sorted(includes):
        if not re.search(rf'^\s*#\s*include\s*[<"]{re.escape(include)}[>"]', text, re.M):
            text = f'#include "{include}"\n' + text
    return re.sub(r"^[ \t]*/\*\s*NON_MATCHING:\s*draft\b[^\n]*\*/[ \t]*\n?", "", text, flags=re.M)


def _records(parser: LayoutParser) -> list[structs.Layout]:
    """Layouts of an already parsed source view, without parsing it again."""
    return [parser.layout(item) for item in parser.aggregates if item.name]


@dataclass(frozen=True)
class Folded:
    """One source after its local layouts moved into shared headers."""

    function: str
    source: str
    headers: list[Edit]
    removed_rows: dict[str, tuple[str, ...]]


def _local_typedefs(
    project: Project, headers: Headers, parsers: list[LayoutParser], records: list[structs.Layout], destination: Path
) -> tuple[Headers, list[Edit], set[tuple[int, int]]]:
    """Move local scalar and callback aliases needed by promoted fields with them."""
    wanted = set(re.findall(r"\b\w+\b", " ".join(field.declaration for record in records for field in record.fields)))
    # Aggregate aliases are emitted with their layouts by fold(), after the
    # necessary tag forwards. A resolved alias can spell `typedef T T;` here;
    # promoting it as a scalar would put it before T's first declaration.
    aggregate_aliases = {name for record in records for name in record.aliases}
    declarations: dict[str, str] = {}
    spans: dict[str, set[tuple[int, int]]] = {}
    for parser in parsers:
        for item in parser.declarations:
            value = parser.source[item.start : item.end]
            local = LayoutParser(value)
            if local.peek() != "typedef":
                continue
            local.take()
            for member in local.declaration(typedef=True):
                if member.name in headers.types or member.name in aggregate_aliases or not isinstance(member.base, str):
                    continue
                declaration = "typedef " + member.declaration
                if member.name in declarations and declarations[member.name] != declaration:
                    structs.held(member.name, "version-dependent local typedef")
                declarations[member.name] = declaration
                spans.setdefault(member.name, set()).add((item.start, item.end))
    selected: dict[str, str] = {}
    while pending := wanted & declarations.keys() - selected.keys():
        for name in sorted(pending):
            selected[name] = declarations[name]
            wanted.update(re.findall(r"\b\w+\b", declarations[name]))
            wanted.update(other for other in declarations if spans[other] & spans[name])
    if not selected:
        return headers, [], set()
    # Declaration order follows the source, including chains of scalar aliases.
    names = sorted(selected, key=lambda name: min(spans[name]))
    before = headers.texts.get(destination, "")
    guard = header_guard(headers, destination)
    after = before or (
        f"#ifndef {guard}\n#define {guard}\n" + _scalar_include(project, headers, records) + "\n#endif\n"
    )
    required = wanted & headers.homes.keys()
    for path in sorted({headers.homes[name] for name in required} - {destination}):
        include = next(path.relative_to(root).as_posix() for root in project.include if path.is_relative_to(root))
        directive = f'#include "{include}"\n'
        if directive.strip() not in after:
            after = directive + after
    after = shared.append(after, "\n".join(selected[name] for name in names) + "\n")
    edit = Edit(destination, before, after, tuple(project.versions))
    context = Headers({**headers.texts, destination: after}, root=headers.root)
    return context, [edit], set().union(*(spans[name] for name in selected))


def fold_source(
    project: Project,
    policy: Host,
    headers: Headers,
    function: str,
    text: str,
    versions: tuple[str, ...],
    *,
    prove_headers: bool = True,
    source_path: Path | None = None,
) -> Folded:
    """Plan aggregate promotion against a shared header context; the context is not changed."""
    from unbake.typemap import declaration_evidence

    authored = text
    text = gbi_recover.import_aliases(
        project, text, headers.texts, sdk_aliases=False, rules=frozenset({"volatile-storage"})
    )
    text, evidence_end = declaration_evidence.inject(project, headers, text, function, versions)
    if evidence_end:
        text = text[:evidence_end] + "/* unbake declaration evidence boundary */\n" + text[evidence_end:]
    text = imports.resolve(project, headers, text, function)
    text = pool_literals.lower(project, function, text, versions)
    parsers = source_views.parsers(project, policy, text, versions, headers)
    text, tag_only = _layout_names(
        project,
        policy,
        function,
        text,
        parsers,
        versions,
        headers,
        source_path=source_path,
        source_line_offset=(
            text.count("\n", 0, text.index("/* unbake declaration evidence boundary */")) + 1 if evidence_end else 0
        ),
    )
    parsers = source_views.parsers(project, policy, text, versions, headers)
    records = [record for parser in parsers for record in _records(parser)]
    records = [replace(record, aliases=()) if record.name in tag_only else record for record in records]
    from unbake.layout import map

    owner = map.load(project).owners.get(function)
    if owner is None:
        raise Held("layout", f"layout.member.{function}: source has no group")
    destination = project.include[0] / owner.header
    context, promoted, moved_spans = _local_typedefs(project, headers, parsers, records, destination)
    edits = fold(
        records,
        project,
        destination=destination,
        prove_headers=prove_headers,
        context=context,
        host=policy,
        republished=project.src / f"{function}.c",
    )
    if promoted:
        by_path = {edit.path: edit for edit in promoted}
        for edit in edits:
            by_path[edit.path] = replace(edit, before=by_path[edit.path].before) if edit.path in by_path else edit
        edits = list(by_path.values())
        context = Headers({**headers.texts, **{edit.path: edit.after for edit in edits}}, root=headers.root)
        # Blank moved typedefs without changing the aggregate edit offsets.
        for start, end in sorted(moved_spans, reverse=True):
            text = text[:start] + "".join("\n" if char == "\n" else " " for char in text[start:end]) + text[end:]
    final = final_source(project, text, parsers, edits, context, destination)
    if promoted:
        include = destination.relative_to(project.include[0]).as_posix()
        if not re.search(rf'^\s*#\s*include\s*[<"]{re.escape(include)}[>"]', final, re.M):
            final = f'#include "{include}"\n' + final
    final = imports.resolve(project, context, final, function, edits=tuple(edits))
    if evidence_end:
        evidence_context = Headers({**headers.texts, **{edit.path: edit.after for edit in edits}}, root=headers.root)
        final, evidence_edits = declaration_evidence.promote(project, evidence_context, final, evidence_end)
        edits.extend(evidence_edits)
    final = gbi_recover.proven(
        project,
        policy,
        project.src / f"{function}.c",
        final,
        {**headers.texts, **{edit.path: edit.after for edit in edits}},
        authored=authored,
    )
    removed: dict[str, tuple[str, ...]] = {}
    for version in versions:
        group = entries.owners(project, policy, project.src / f"{function}.c", version, text=text)
        if len(group) < 2:
            continue
        _, lines, segments = split.layout(project.version(version).split)
        paths = {row.path for row in group[1:]}
        removed[version] = tuple(lines[row.line] for segment in segments for row in segment.rows if row.path in paths)
    from unbake.layout import apply

    if edits or destination in headers.texts:
        final = apply.source(project, final, function, {edit.path: edit.after.encode() for edit in edits})
    else:
        from unbake.layout import redeclarations

        final = redeclarations.strip(final, apply.imported(final, project.include[0], {}))
    return Folded(function, final, edits, removed)


def folded_edits(
    project: Project, policy: Host, function: str, text: str, versions: tuple[str, ...], *, prove_headers: bool = True
) -> list[Edit]:
    """The folded source, the header edits and the split rows the fold absorbed (land converts F's own row)."""
    folded = fold_source(project, policy, Headers.read(project), function, text, versions, prove_headers=prove_headers)
    path = project.src / f"{function}.c"
    edits = [Edit(path, path.read_text() if path.exists() else "", folded.source, versions)]
    for version, removed in folded.removed_rows.items():
        split_path = project.version(version).split
        before = split_path.read_text()
        after = _remove_rows(before, removed)
        if after != before:
            edits.append(Edit(split_path, before, after, (version,)))
    return [*folded.headers, *edits]


def _remove_rows(text: str, removed: Iterable[str]) -> str:
    for line in removed:
        text = text.replace(line, "", 1)
    return text


def _layout_names(
    project: Project,
    policy: Host,
    function: str,
    text: str,
    parsers: list[LayoutParser],
    versions: tuple[str, ...],
    headers: Headers,
    *,
    source_path: Path | None = None,
    source_line_offset: int = 0,
) -> tuple[str, set[str]]:
    """Rewrite active type tokens using complete layout evidence, before merging fields."""
    tag_only = headers.tag_only
    resolved_tags: set[str] = set()
    sdk = headers.sdk
    replacements: dict[tuple[int, int], str] = {}
    redundant: dict[tuple[int, int], str] = {}
    renames: set[tuple[str, str]] = set()
    member_renames: set[tuple[str, str]] = set()

    def renamed_members(old: tuple[structs.Field, ...], new: tuple[structs.Field, ...], left: str, right: str) -> None:
        for field, target in zip(old, new, strict=True):
            before, after = f"{left}.{field.name}", f"{right}.{target.name}"
            if field.name != target.name:
                member_renames.add((before, after))
            renamed_members(field.fields, target.fields, before, after)

    shared_aliases = {name: type_ for value in headers.texts.values() for name, type_ in alias_types(value).items()}

    with ExitStack() as cleanup:
        context_project: Project | None = None

        def effective_project() -> Project:
            nonlocal context_project
            if context_project is None:
                temporary = cleanup.enter_context(tempfile.TemporaryDirectory(prefix="match-types-"))
                roots = source_views.header_includes(project, headers, Path(temporary))
                context_project = replace(project, work_include=tuple(roots))
            return context_project

        def typed_context(version: str) -> str:
            return source_views.typed_context(
                project, policy, headers, version, source_context=True, context_project=effective_project()
            )

        def expanded_context(parser: LayoutParser, version: str) -> rewrite_view.View:
            return rewrite_view.prepare(
                effective_project(),
                policy,
                text,
                version,
                source_path or project.src / f"{function}.c",
                contents=source_views.authored_contents(project, headers, effective_project()),
            )

        for index, parser in enumerate(parsers):
            local_aliases = {}
            for start, end in sorted({(item.start, item.end) for item in parser.declarations}):
                local_aliases.update(alias_types(parser.source[start:end]))
            callbacks = callback_renames(local_aliases, shared_aliases, function)
            # Validate canonical scalar names before rewriting any aggregate alias.
            scalar_edits(project, parser, headers)
            records = _records(parser)
            resolution = headers.index.resolve([record for record in records if record.name not in sdk], function)
            resolved_tags.update(target for target, _ in resolution.values() if target in tag_only)
            if not resolution and not callbacks:
                continue
            planned = type_rewrite.edits(
                parser,
                partial(typed_context, versions[index]),
                resolution,
                tag_only,
                cache_root=project.cache,
                typedef_renames=callbacks,
                preprocess=partial(expanded_context, parser, versions[index]),
                source_path=source_path or project.src / f"{function}.c",
                source_line_offset=source_line_offset,
                source_text=text,
            )
            for span, target in planned.items():
                if span in replacements and replacements[span] != target:
                    structs.held(function, "version-dependent layout rename at the same source token")
                replacements[span] = target
            # A layout that keeps its name may absorb a same-source duplicate,
            # whose forward typedef and definition are then both dropped.
            local = {record.name for record in records if resolution.get(record.name, (record.name,))[0] == record.name}
            defined: set[str] = set()
            for record in records:
                if record.name not in resolution:
                    continue
                target, _ = resolution[record.name]
                if target in defined:
                    # A forward typedef and its definition both name this record.
                    spans = [
                        (item.start, item.end)
                        for item in parser.declarations
                        if getattr(item.base, "start", None) == record.start
                    ]
                    for number, span in enumerate(spans):
                        keep = record.aliases and number == 0 and target not in local
                        redundant[span] = f"typedef {record.kind} {target} {target};" if keep else ""
                defined.add(target)
            for name, (target, _) in resolution.items():
                if name != target:
                    renames.add((name, target))
            for record in records:
                if record.name in resolution:
                    target, evidence = resolution[record.name]
                    renamed_members(record.fields, evidence.fields, record.name, target)
    replacements = {
        span: target
        for span, target in replacements.items()
        if not any(start <= span[0] and span[1] <= end for start, end in redundant)
    }
    replacements.update(redundant)
    for (start, end), target in sorted(replacements.items(), reverse=True):
        text = text[:start] + target + text[end:]
    for name, target in sorted(renames):
        reporting.learn(f"OK(types): {function}: rename {name} -> {target} (shared layout)")
    for name, target in sorted(member_renames):
        reporting.learn(f"OK(types): {function}: rename {name} -> {target} (shared member layout)")
    return text, resolved_tags
