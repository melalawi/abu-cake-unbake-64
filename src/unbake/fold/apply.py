"""Fold one draft against the shared headers: the folded source, the header texts it needs, the split edits.

A draft sees build/work/FUNC/include/ before include/. Its group header is copied there before folding,
so fold edits a private copy; tidy leaves those copies in the work dir and land publishes them.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from unbake import atomic as atomic_files
from unbake.config import Held, Host, Project, draft_view
from unbake.decomp import checks, gbi
from unbake.fold import declarations, notes, self_prototype
from unbake.layout import map as layout_map
from unbake.layout import split
from unbake.layout.split import Edit
from unbake.process import named as cause_named
from unbake.work.attempts import Attempt


@dataclass(frozen=True)
class Folded:
    function: str
    source: str
    headers: dict[str, str]
    split_edits: tuple[Edit, ...]
    notes: tuple[str, ...] = field(default=())
    source_edits: tuple[Edit, ...] = field(default=())
    contract: self_prototype.Contract | None = None


def view(project: Project, function: str) -> Project:
    """The draft view, with the function's group header copied into the work include dir once."""
    drafted = draft_view(project, function)
    owner = layout_map.load(project).owners.get(function)
    if owner is None:
        raise Held(
            cause_named(
                f"layout.member.{function}",
                f"layout.member.{function}: function has no group in layout.toml",
                owner="fold.apply",
                stage="layout",
            )
        )
    work_root = drafted.work_include[0]
    shared = project.include[-1] / owner.header
    private = work_root / owner.header
    if shared.is_file():
        book = project.work / function / ".header-bases.json"
        bases = json.loads(book.read_text()) if book.is_file() else {}
        if not isinstance(bases, dict) or any(
            not isinstance(k, str) or not isinstance(v, str) for k, v in bases.items()
        ):
            raise Held(
                cause_named(
                    "fold.header_bases",
                    f"fold.header_bases: {book}: expected header digests",
                    owner="fold.apply",
                    stage="fold",
                )
            )
        shared_digest = hashlib.sha256(shared.read_bytes()).hexdigest()
        private_digest = hashlib.sha256(private.read_bytes()).hexdigest() if private.is_file() else None
        # Untouched copies follow the current shared header after another land.
        # A staged edit keeps its private bytes. Older untracked copies establish
        # a baseline only when they already equal the current shared content.
        if private_digest is None or private_digest == bases.get(owner.header):
            if private_digest != shared_digest:
                private.parent.mkdir(parents=True, exist_ok=True)
                atomic_files.copyfile(shared, private)
            private_digest = shared_digest
        if private_digest == shared_digest and bases.get(owner.header) != shared_digest:
            bases[owner.header] = shared_digest
            atomic_files.text(book, json.dumps(bases, sort_keys=True) + "\n")
    link_private_includes(project, function)
    return drafted


def link_private_includes(project: Project, function: str) -> None:
    """A private header copy's parent-relative includes ("../types.h") resolve as they do beside its shared
    original: each missing target is linked to the shared file. Links are never private headers."""
    root = project.work / function / "include"
    for private in sorted(root.rglob("*.h")) if root.is_dir() else ():
        if private.is_symlink():
            continue
        shared = project.include[-1] / private.relative_to(root)
        for name in re.findall(r'^[ \t]*#[ \t]*include[ \t]*"(\.\./[^"]+)"', private.read_text(), re.M):
            mirror = Path(os.path.normpath(private.parent / name))
            target = Path(os.path.normpath(shared.parent / name))
            if mirror.is_relative_to(root) and target.is_file() and not mirror.exists():
                mirror.parent.mkdir(parents=True, exist_ok=True)
                atomic_files.before_write(mirror)
                mirror.symlink_to(target)


def relative_header(project: Project, path: Path) -> str | None:
    for root in project.include:
        if path.is_relative_to(root):
            return path.relative_to(root).as_posix()
    return None


def fold(
    project: Project,
    host: Host,
    function: str,
    text: str,
    *,
    versions: tuple[str, ...] | None = None,
    exact_entry: Attempt | None = None,
) -> Folded:
    """Lower GBI, fold shared types and plan the publication edits, without writing project files."""
    drafted = view(project, function)
    versions = split.holding_versions(project, function) if versions is None else versions
    lowered = gbi.prepare(drafted, text, gbi.microcode(drafted))
    source = lowered.source
    if "gbi" in lowered.headers:
        source = gbi.install(drafted) + source
    if "abi" in lowered.headers:
        source = gbi.install_audio(drafted) + source
    with notes.collect() as learned:
        edits = declarations.folded_edits(
            drafted,
            host,
            function,
            source,
            versions,
            prove_headers=False,
            exact_entry=exact_entry,
        )
    headers: dict[str, str] = {}
    split_edits = []
    folded = None
    for edit in edits:
        if edit.path == drafted.src / f"{function}.c":
            folded = edit.after
            continue
        name = relative_header(drafted, edit.path)
        if name is not None:
            headers[name] = edit.after
        else:
            split_edits.append(edit)
    if folded is None:
        raise Held(
            cause_named(
                "fold.source", f"fold.source: {function}: fold produced no source", owner="fold.apply", stage="fold"
            )
        )
    blockers = [finding for finding in checks.run(folded) if finding.fakematch is None]
    if blockers:
        raise Held(
            cause_named(
                "fold.apply.fold",
                f"fold.source_rules: {function} breaks the source rules: "
                + "; ".join(checks.plain(f) for f in blockers),
                owner="fold.apply",
                stage="fold",
            )
        )
    from unbake.fold import shared_consumers
    from unbake.layout import header_loss, header_step
    from unbake.layout.structs_fold import _prove_includers
    from unbake.typemap import namespace

    effective = {**private_headers(project, function), **headers}
    source_edits = shared_consumers.plan(project, host, function, effective)
    contract = edits.contract
    if contract is not None:
        from unbake.layout import redeclarations

        # Include-only reachability cannot find a source-local extern or a
        # function-pointer use. Bind every current spelled consumer to the
        # proposed canonical provider, then use the existing all-holder native
        # consumer proof. Incompatible authored views are deliberately kept;
        # their compiler conflict must refuse the entire replacement.
        providers = [name for name, body in sorted(headers.items()) if function in redeclarations.catalog(body)]
        if not providers:
            raise Held(
                cause_named(
                    "land.own_contract",
                    f"land.own_contract: {function}: replacement has no self-contained provider",
                    owner="fold",
                    stage="land",
                )
            )
        include = f'#include "{providers[0]}"\n'
        pending = {edit.path: edit for edit in source_edits}
        for path in sorted(project.src.glob("*.c")):
            if path.stem == function:
                continue
            before = path.read_text()
            code = re.sub(r"/\*.*?\*/|//[^\n]*", " ", before, flags=re.S)
            code = re.sub(r"^\s*#\s*include[^\n]*", "", code, flags=re.M)
            occurrences = list(re.finditer(r"\b" + re.escape(function) + r"\b", code))
            if not occurrences:
                continue
            if any(not re.match(r"\s*\(", code[match.end() :]) for match in occurrences):
                raise Held(
                    cause_named(
                        "land.own_contract",
                        f"land.own_contract: {function}: indirect consumer {path} requires explicit proof",
                        owner="fold",
                        stage="land",
                    )
                )
            current = pending.get(path)
            after = before if current is None else current.after
            if include not in after:
                after = include + after
            # Even an existing include must retain a source proof obligation.
            pending[path] = Edit(path, before, after, tuple(split.holding_versions(project, path.stem)))
        source_edits = tuple(pending.values())
    from unbake.layout.header_context import Headers

    installed = Headers.contents(project)
    contracts = namespace.project_declarations(project, installed, texts=(folded,))
    canonical_edits = namespace.consumer_edits(project, contracts, function, source_edits)
    identity_changed = canonical_edits != source_edits or any(
        contracts.rewrite(text) != text for text in installed.values()
    )
    source_edits = canonical_edits
    header_edits = [
        Edit(
            project.include[-1] / name,
            (project.include[-1] / name).read_text() if (project.include[-1] / name).is_file() else "",
            text,
            versions,
        )
        for name, text in effective.items()
        if not (project.include[-1] / name).is_file() or (project.include[-1] / name).read_text() != text
    ]
    if header_edits:
        _prove_includers(project, [*header_edits, *source_edits], host, project.src / f"{function}.c")
    if identity_changed:
        header_step.validate(
            project,
            host,
            {edit.path: edit.after.encode() for edit in [*header_edits, *source_edits]},
            prove_all=True,
            preproved=frozenset({function}),
        )
    header_loss.check(
        project,
        {
            project.src / f"{function}.c": folded.encode(),
            **{project.include[-1] / n: t.encode() for n, t in headers.items()},
            **{edit.path: edit.after.encode() for edit in source_edits},
        },
        policy=host,
    )
    return Folded(function, folded, headers, tuple(split_edits), tuple(learned), source_edits, contract)


def private_headers(project: Project, function: str) -> dict[str, str]:
    """Every header in the draft's work include dir, by relative name."""
    root = project.work / function / "include"
    if not root.is_dir():
        return {}
    return {
        path.relative_to(root).as_posix(): path.read_text()
        for path in sorted(root.rglob("*.h"))
        if not path.is_symlink()
    }
