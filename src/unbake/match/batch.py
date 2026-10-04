"""Publish authored C in one transaction proved by one cartridge build.

Admission reads names, owning rows and source rules only. Every source folds
its local layouts against one shared header context. One build compiles each
source once per containing version; when a version differs, that build's
objects name the culprits, the passing subset is relinked from the same
objects, and only the passing subset publishes. A current exact try receipt
supplies a measured compiler choice; no receipt is required.
"""

from __future__ import annotations

import hashlib
import json
import re
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import toml  # type: ignore[import-untyped]

from unbake.decomp import checks, drafts, type_context, work
from unbake.decomp.needs import SymbolNeed
from unbake.layout import split, split_apply
from unbake.layout.header_context import Headers
from unbake.match import (
    attribution,
    batch_fold,
    data_symbols,
    forked,
    incremental,
    relink,
    reporting,
    staging,
)
from unbake.match.common import atomic, held
from unbake.match.publication import swap
from unbake.project import build, compiler_choice, config, makefile, setup, workspace
from unbake.project.config import Held, Policy, Project
from unbake.project_tools import atomic as atomic_files
from unbake.report import progress
from unbake.typemap import storage

# Rules the fold resolves: they are judged on the folded text, not on admission.
FOLDED_RULES = frozenset(
    {
        "invented-struct",
        "local-type-copy",
        "raw-offset",
        "raw-gfx",
        "local-gbi-macro",
        "volatile-storage",
        "local-include",
    }
)


@dataclass
class Candidate:
    function: str
    source: Path
    content: bytes
    sha256: str
    versions: tuple[str, ...]
    matched: bool
    compiler: dict[str, Any] = field(default_factory=dict)
    final: str = ""
    removed_rows: dict[str, tuple[str, ...]] = field(default_factory=dict)
    symbol_needs: list[SymbolNeed] = field(default_factory=list)
    republication: bool = False
    compiled: bool = False


def publish(project: Project, policy: Policy, sources: list[Path]) -> list[str]:
    """Admit, fold, prove and publish; refusals are named per source."""
    names = [source.stem for source in sources]
    if len(set(names)) != len(names):
        held("submit.source: duplicate function names in --batch")
    with (
        reporting.session(project),
        build.lock(project),
        forked.session(policy.cores, max(len(sources), len(project.versions))),
    ):
        project = config.load(project.root)
        setup.refresh_helpers(project)
        if not sources:
            return _baseline(project, policy)
        return _publish(project, policy, [source.resolve() for source in sources])


def _baseline(project: Project, policy: Policy) -> list[str]:
    """Prove the unchanged project through the same retained-object link boundary."""
    # Keep the previous recipe as provenance: refreshing implementation helpers
    # must not bless different compiler flags or other changed build inputs.
    if json.loads((project.tools / "build.json").read_text()) != makefile.description(project):
        held("submit.baseline_inputs: build recipe changed; run setup before an empty submit")
    with ExitStack() as holds:
        generations = {
            version: staging.generation(
                project, version, holds.enter_context(build.pin_current(project, version)), holds, retained=True
            )
            for version in project.versions
        }
        dirty = {
            version: incremental.changed_sources(project, project, generation, version)
            for version, generation in generations.items()
        }
        if any(dirty.values()):
            held(
                "submit.baseline_inputs: published source inputs changed; "
                "submit the changed sources or run make -j4 check"
            )
        with ThreadPoolExecutor(max_workers=min(policy.cores, len(generations))) as pool:
            futures = {
                version: pool.submit(relink.prove, project, policy, version, generation, None, extracted=True)
                for version, generation in generations.items()
            }
            results = {version: future.result() for version, future in futures.items()}
        failed = [version for version, result in results.items() if not result.ok]
        if failed:
            held("submit.dependencies: unchanged baseline failed on " + ", ".join(failed))
        return [f"OK(submit): {version}: {result.sha1_line}" for version, result in results.items()]


def _publish(project: Project, policy: Policy, sources: list[Path]) -> list[str]:
    receipts = reporting.Receipts()
    started = staging.fingerprint(project, project.root)
    with reporting.phase("admission", sources=len(sources)):
        candidates = _admission(project, policy, sources, receipts)
    if not candidates:
        return receipts
    project.build.mkdir(parents=True, exist_ok=True)
    with workspace.temporary(project, prefix="submit-", directory=project.build) as temporary:
        work = Path(temporary)
        with ExitStack() as holds:
            current: dict[str, Path] = {}
            with build.lock(project):
                for version in project.versions:
                    current[version] = holds.enter_context(build.pin(build.current_generation(project, version)))
            with reporting.phase("reuse_objects", sources=len(candidates)):
                reused = incremental.reusable_sources(
                    project, current, {c.function: c.versions for c in candidates if c.republication}
                )
                for candidate in candidates:
                    candidate.compiled = candidate.function in reused
                reporting.record("reused_objects", sources=sorted(reused))
            with reporting.phase("stage"):
                tree = work / "tree"
                staging.copy_tree(project, project.root, tree, skip=("docs",), assembly=False, linked=True)
                local_policy = project.tools / "clone-policy.toml"
                if local_policy.is_file():
                    atomic_files.copy2(local_policy, tree / local_policy.relative_to(project.root))
                staged = staging.project_at(project, tree)
                if not all(candidate.compiled for candidate in candidates):
                    staging.write_staged(staged, staging.helper_edits(staged))
                base = _Base(staged, policy)
            with reporting.phase("fold", sources=len(candidates)):
                candidates = _fold(staged, policy, candidates, receipts)
            if not candidates:
                return receipts
            candidates = _row_owners(base, candidates, receipts)
            if not candidates:
                return receipts
            with reporting.phase("data_symbols"):
                _materialize(staged, base, candidates)
                candidates = _data_symbols(staged, policy, candidates, receipts)
            if not candidates:
                return receipts
            _materialize(staged, base, candidates)
            with reporting.phase("type_preflight", sources=len(candidates)):
                candidates = _type_preflight(staged, policy, candidates, receipts)
            if not candidates:
                return receipts
            _materialize(staged, base, candidates)
            generations: dict[str, Path] = {}
            versions = list(project.versions)
            for version in versions:
                generations[version] = staging.generation(
                    project,
                    version,
                    current[version],
                    holds,
                    retained=True,
                    borrowed=all(candidate.compiled for candidate in candidates),
                )
                for candidate in candidates:
                    if candidate.republication and not candidate.compiled and version in candidate.versions:
                        (generations[version] / "obj/src" / f"{candidate.function}.built").unlink(missing_ok=True)
            with reporting.phase("proof", sources=len(candidates)):
                extracted = {v: staged.version(v).split.read_text() for v in versions}
                faults = incremental.prepare(
                    project,
                    staged,
                    policy,
                    generations,
                    base.splits,
                    submitted={candidate.function for candidate in candidates},
                )
                if faults is not None:
                    for name, reasons in attribution.diagnose(
                        staged,
                        versions,
                        generations,
                        {candidate.function for candidate in candidates},
                        (project, current),
                    ).items():
                        for reason in reasons:
                            if reason not in faults.setdefault(name, []):
                                faults[name].append(reason)
                    unrelated = faults.keys() - {candidate.function for candidate in candidates}
                    if unrelated:
                        held("submit.dependencies: " + "; ".join(faults[next(iter(unrelated))]))
                    passing = [candidate for candidate in candidates if candidate.function not in faults]
                    _materialize(staged, base, passing)
                    results = _relink(staged, policy, generations, extracted)
                else:
                    staging.copy_assembly(project, staged)
                    for version in versions:
                        staging.independent_objects(generations[version])
                        staging.chunk_stale_sources(generations[version], staged.tools, staged.version(version).symbols)
                    results = build.build(project, policy, versions, tree=tree, generation_for=generations.__getitem__)
                    faults = attribution.diagnose(
                        staged,
                        versions,
                        generations,
                        {candidate.function for candidate in candidates},
                        (project, current),
                    )
                    if faults:
                        # Make cannot link a failed compile. Restore those original
                        # providers and link every successful object before naming
                        # this round's compile, placement, binding and byte faults.
                        passing = [candidate for candidate in candidates if candidate.function not in faults]
                        _materialize(staged, base, passing)
                        results = _relink(staged, policy, generations, extracted)
                candidates, sha1 = _isolate(
                    project, staged, base, policy, candidates, generations, results, receipts, faults, current
                )
            if not candidates:
                return receipts
            forked.finish()
            with reporting.phase("publication", sources=len(candidates)):
                candidates, sha1, followups = _publish_survivors(
                    project, staged, base, policy, candidates, current, generations, started, receipts, sha1
                )
                receipts.extend(followups)
            if not candidates:
                return receipts
            if all((generation / "obj").is_symlink() for generation in generations.values()):
                holds.close()
                for version, generation in generations.items():
                    build.discard_generation(generation)
                    sha1[version] = f"{current[version] / f'{project.name}.{version}.z64'}: OK"
            receipts.extend(
                f"OK(match): {candidate.function} matched on VERSION {', '.join(candidate.versions)}"
                if candidate.matched
                else (
                    f"OK(submit): {candidate.function} published as NON_MATCHING; "
                    f"asm rows retained on {', '.join(candidate.versions)}"
                )
                for candidate in candidates
            )
            receipts.extend(f"OK(submit): {version}: {line}" for version, line in sha1.items())
        return receipts


class _Inputs:
    """Trial rows and split owners read once for a whole admission."""

    def __init__(self, project: Project, policy: Policy, sources: list[Path] | None = None) -> None:
        # The build lock holds these inputs still; a refusal repeats per source.
        self.owners: dict[str, Any] | None = None
        self.trials: dict[str, list[Any]] | None = None
        proven = project.build / "types/proven.json"
        self.proven = storage.read(proven, "types.feedback") if proven.is_file() else {}
        if self.proven:
            storage.validate_identity(project, self.proven, "types.feedback")
        try:
            self.owners = {v: split.owners_by_alias(project, v) for v in project.versions}
            self.trials = (
                {}
                if sources is not None and all(source == project.src / source.name for source in sources)
                else drafts.Store(policy, project).by_function()
            )
        except Held:
            pass


def _admission(project: Project, policy: Policy, sources: list[Path], receipts: list[str]) -> list[Candidate]:
    """Admit every source on all cores; candidates and refusals keep source order."""
    shared = project, policy, _Inputs(project, policy, sources)
    candidates = []
    for source, (lines, outcome) in zip(sources, forked.ordered(_admitted, shared, sources, policy.cores), strict=True):
        for line in lines:
            reporting.learn(line)
        if isinstance(outcome, str):
            receipts.append(f"HELD(submit): {source.stem}: {outcome}")
        elif outcome is None:
            receipts.append(f"OK(submit): {source.stem}: published source unchanged")
        else:
            candidates.append(outcome)
    return candidates


def _admitted(shared: tuple[Project, Policy, _Inputs], source: Path) -> Candidate | str | None:
    try:
        return _admit(*shared, source)
    except Held as error:
        return error.reason


def _admit(project: Project, policy: Policy, inputs: _Inputs, source: Path) -> Candidate | None:
    function = source.stem
    if source.suffix != ".c" or not re.fullmatch(r"[A-Za-z_]\w*", function):
        held(f"submit.source: {source} must be named <function>.c")
    try:
        content = source.read_bytes()
        text = content.decode("utf-8")
    except (OSError, UnicodeError) as error:
        held(f"submit.source: {source}: {error}")
    versions = split.holding_versions(project, function, inputs.owners)
    blockers = [f for f in checks.run(text) if f.fakematch is None and f.rule not in FOLDED_RULES]
    if blockers:
        held("submit.source_rules: " + "; ".join(checks.message(finding) for finding in blockers))
    from unbake.decomp import gbi_recover

    gbi_recover.preflight(project, policy, source, text)
    sha = drafts.source_identity(content)
    trials = (
        inputs.trials.get(function, []) if inputs.trials is not None else drafts.Store(policy, project).rows(function)
    )
    rows = [row for row in trials if row["source_sha256"] == sha]
    latest = rows[-1] if rows else None
    destination = project.src / f"{function}.c"
    published = destination.is_file() and not drafts.is_partial(destination.read_text())
    republication = published and source.resolve() == destination.resolve()
    if republication:
        proven = project.build / "types/proven.json"
        if proven.is_file():
            value = getattr(inputs, "proven", None)
            if value is None:
                value = storage.read(proven, "types.feedback")
                storage.validate_identity(project, value, "types.feedback")
            record = value.get("records", {}).get(function, {})
            if (
                record.get("source") == str(destination.relative_to(project.root))
                and record.get("source_sha256") == hashlib.sha256(content).hexdigest()
            ):
                return None
        # Re-prove a published unit as matched C regardless of an older trial.
        return Candidate(function, source, content, sha, versions, True, republication=True)
    if latest is not None and not latest["identical_everywhere"]:
        from unbake.match.nonmatching import admit

        try:
            work.current_trial(project, policy, source, list(versions), latest["work"])
        except Held as error:
            if not error.reason.endswith("changed since latest try"):
                raise
            # An old fuzzy refusal cannot veto a fresh exact cartridge proof.
            # Drop its compiler choice too; the current configured inputs own it.
            latest = None
        else:
            manifest = admit(project, policy, source)
            return Candidate(function, source, content, sha, versions, False, dict(manifest["compiler_evidence"]))
    if published:
        held(f"submit.source: {destination}: matched source already exists")
    evidence = dict(latest["work"].get("compiler_evidence", {})) if latest is not None else {}
    if evidence and evidence.get("configured") != project.compiler_reference(function):
        evidence = {}
    return Candidate(function, source, content, sha, versions, True, evidence)


class _Base:
    """Staged inputs before any candidate edit; materialization starts here."""

    def __init__(self, staged: Project, policy: Policy) -> None:
        self.policy = policy
        self.layout_map = (staged.root / "layout.toml").read_text()
        self.symbols = {v: staged.version(v).symbols.read_text() for v in staged.versions}
        self.splits = {v: staged.version(v).split.read_text() for v in staged.versions}
        self.config = (staged.root / "config.toml").read_text()
        self.sources = {path.name: path.read_text() for path in staged.src.glob("*.c")}
        exclusions = staged.root / "unbake-exclusions.json"
        self.exclusions = exclusions.read_text() if exclusions.is_file() else None
        self.exclusion_aliases: dict[str, set[str]] = {}
        if self.exclusions is not None:
            from unbake.decomp import exclusions as reserved

            reserved.load(staged)
            for version in staged.versions:
                for row in split.functions(staged, version):
                    self.exclusion_aliases.setdefault(Path(row.path).name, set()).update((row.name, *row.aliases))


def _fold(staged: Project, policy: Policy, candidates: list[Candidate], receipts: list[str]) -> list[Candidate]:
    """Fold every source against one context; a refused source leaves the context unchanged."""
    if all(candidate.compiled for candidate in candidates):
        for candidate in candidates:
            candidate.final = candidate.content.decode("utf-8")
        return candidates
    headers = Headers.read(staged)
    base = dict(headers.texts)
    retained = [candidate for candidate in candidates if candidate.compiled]
    for candidate in retained:
        candidate.final = candidate.content.decode("utf-8")
    pending = [candidate for candidate in candidates if not candidate.compiled]
    accepted = set()
    for candidate, folded in batch_fold.fold(staged, policy, headers, pending, receipts):
        accepted.add(candidate.function)
        candidate.final = folded.source
        candidate.removed_rows = folded.removed_rows
    changed_headers = {path: text for path, text in headers.texts.items() if base.get(path) != text}
    for path, text in changed_headers.items():
        split_apply.write(path, text)
    from unbake.layout import index

    index.update(staged, changed_headers)
    return [candidate for candidate in candidates if candidate.compiled or candidate.function in accepted]


def _materialize(staged: Project, base: _Base, candidates: list[Candidate]) -> None:
    """Render sources, rows, compiler units and exclusions for exactly these candidates."""
    if candidates and all(candidate.compiled for candidate in candidates):
        return
    selected = {candidate.function: candidate for candidate in candidates}
    for version, text in base.symbols.items():
        split_apply.write(staged.version(version).symbols, text)
    needs = [need for candidate in candidates for need in candidate.symbol_needs]
    for edit in data_symbols.resolve(needs, staged, base.policy):
        split_apply.write(edit.path, edit.after)
    for path in staged.src.glob("*.c"):
        if path.stem not in selected and base.sources.get(path.name) != path.read_text():
            if path.name in base.sources:
                split_apply.write(path, base.sources[path.name])
            else:
                path.unlink()
    for candidate in candidates:
        text = candidate.final
        if not candidate.matched:
            text = "#ifdef NON_MATCHING\n" + drafts.canonical_source(text.encode()).decode().rstrip("\n") + "\n#endif\n"
        split_apply.write(staged.src / f"{candidate.function}.c", text)
    for version in staged.versions:
        rendered = _rows(base.splits[version], version, [c for c in candidates if c.matched and version in c.versions])
        path = staged.version(version).split
        if path.read_text() != rendered:
            split_apply.write(path, rendered)
    from unbake.layout import map as ownership

    layout_path = staged.root / "layout.toml"
    if not layout_path.is_file() or layout_path.read_text() != base.layout_map:
        split_apply.write(layout_path, base.layout_map)
    folded_members = {
        Path(split.plain(row["path"])).name
        for candidate in candidates
        for lines in candidate.removed_rows.values()
        for line in lines
        if (row := split.ROW.fullmatch(line)) is not None
    }
    if folded_members:
        current = ownership.catalog(staged)
        removed: dict[str, tuple[str, ...]] = {name: () for name in folded_members if name not in current}
        if removed:
            ownership.edit_members(staged, removed)
    chosen = [c.compiler for c in candidates if c.matched and c.compiler.get("exact_candidates")]
    config_text = toml.dumps(compiler_choice.fold_units(toml.loads(base.config), chosen)) if chosen else base.config
    if (staged.root / "config.toml").read_text() != config_text:
        split_apply.write(staged.root / "config.toml", config_text)
    _recipe(staged)
    exclusions = staged.root / "unbake-exclusions.json"
    if base.exclusions is not None:
        # Folded secondary entries are published by their owning C unit. They
        # must leave the manifest before validation against the merged rows.
        released = {candidate.function for candidate in candidates if candidate.matched}
        for candidate in candidates:
            if candidate.matched:
                for lines in candidate.removed_rows.values():
                    for line in lines:
                        row = split.ROW.fullmatch(line)
                        if row is not None:
                            released.add(Path(split.plain(row["path"])).name)
        released.update(alias for name in tuple(released) for alias in base.exclusion_aliases.get(name, set()))
        manifest = json.loads(base.exclusions)
        manifest["functions"] = [name for name in manifest["functions"] if name not in released]
        split_apply.write(exclusions, json.dumps(manifest, indent=2) + "\n")
        from unbake.decomp import exclusions as reserved

        for edit in reserved.publication_edit(staged, {c.function for c in candidates if c.matched}):
            split_apply.write(edit.path, edit.after)


def _row_owners(base: _Base, candidates: list[Candidate], receipts: list[str]) -> list[Candidate]:
    """Refuse a source whose row is missing, duplicated or claimed by another source's fold."""
    refused: dict[str, str] = {}
    for version, text in base.splits.items():
        removed = {
            line: candidate.function for candidate in candidates for line in candidate.removed_rows.get(version, ())
        }
        rows: dict[str, list[str]] = {}
        for line in text.splitlines(keepends=True):
            row = split.ROW.fullmatch(line)
            if row is not None and row["kind"] in ("asm", "c"):
                rows.setdefault(Path(split.plain(row["path"])).name, []).append(line)
        for candidate in candidates:
            if not candidate.matched or version not in candidate.versions:
                continue
            own = rows.get(candidate.function, [])
            kind = "c" if candidate.republication else "asm"
            if len(own) != 1 or split.ROW.fullmatch(own[0])["kind"] != kind:  # type: ignore[index]
                refused.setdefault(candidate.function, f"submit.row: VERSION {version} requires one {kind} row")
            elif own[0] in removed and removed[own[0]] != candidate.function:
                refused.setdefault(
                    candidate.function, f"submit.row: VERSION {version} row is folded by {removed[own[0]]}"
                )
    for candidate in candidates:
        if candidate.function in refused:
            receipts.append(f"HELD(submit): {candidate.function}: {refused[candidate.function]}")
    return [candidate for candidate in candidates if candidate.function not in refused]


def _rows(text: str, version: str, candidates: list[Candidate]) -> str:
    """Switch each candidate's assembly row to C and drop rows its source now owns."""
    wanted = {candidate.function: candidate for candidate in candidates}
    removed = {line for candidate in candidates for line in candidate.removed_rows.get(version, ())}
    lines = text.splitlines(keepends=True)
    seen: set[str] = set()
    result = []
    for line in lines:
        if line in removed:
            continue
        row = split.ROW.fullmatch(line)
        name = Path(split.plain(row["path"])).name if row is not None else None
        if row is not None and name in wanted and row["kind"] in ("asm", "c"):
            kind = "c" if wanted[name].republication else "asm"
            if row["kind"] != kind or name in seen:
                held(f"{name}: VERSION {version} requires one {kind} row")
            seen.add(name)
            line = split.replace_row(line, row, kind="c", path=name)
        result.append(line)
    missing = set(wanted) - seen
    if missing:
        held(f"{', '.join(sorted(missing))}: VERSION {version} asm row missing")
    return "".join(result)


def _recipe(staged: Project) -> None:
    """Render the build recipe for the staged configuration and pin it in the manifest."""
    project = config.load(staged.root)
    recipe = staged.tools / "build.json"
    rendered = json.dumps(makefile.description(project), sort_keys=True, indent=2) + "\n"
    if recipe.read_text() == rendered:
        return
    checksum = staged.tools / "compiler.sha256"
    name = recipe.relative_to(staged.root).as_posix()
    lines = checksum.read_text().splitlines(keepends=True)
    entries = [i for i, line in enumerate(lines) if line.strip().split(maxsplit=1)[1:] == [name]]
    if len(entries) != 1:
        held("submit.compiler_recipe: expected one verified build recipe manifest entry")
    lines[entries[0]] = f"{hashlib.sha256(rendered.encode()).hexdigest()}  {name}\n"
    split_apply.write(recipe, rendered)
    split_apply.write(checksum, "".join(lines))
    setup.refresh_helpers(project)


def _data_symbols(staged: Project, policy: Policy, candidates: list[Candidate], receipts: list[str]) -> list[Candidate]:
    """Place unknown data a source declares from its owning ROM relocations, in parallel."""
    if all(candidate.compiled for candidate in candidates):
        return candidates
    known = {v: set(split.symbols(staged.version(v).symbols)[1]) for v in staged.versions}
    work = staged.work / "data-symbols"
    work.mkdir(parents=True, exist_ok=True)
    jobs: list[tuple[Candidate, str]] = []
    for candidate in candidates:
        if not candidate.matched or candidate.compiled:
            continue
        declared = set(re.findall(r"\bextern\b[^;()]*?\b([A-Za-z_]\w*)\s*(?:\[[^\]]*\]\s*)*;", candidate.final))
        jobs.extend(
            (candidate, version) for version in candidate.versions if declared - known[version] - {candidate.function}
        )

    # One interpreter per chunk compiles through the content cache the proof build reuses.
    chunks: list[tuple[str, list[Candidate]]] = []
    for version in staged.versions:
        members = [candidate for candidate, v in jobs if v == version]
        size = max(1, len(members))
        chunks.extend((version, members[i : i + size]) for i in range(0, len(members), size))

    def compile_chunk(chunk: tuple[str, list[Candidate]]) -> list[tuple[Candidate, list[SymbolNeed] | str]]:
        version, members = chunk
        output = work / version
        failures = build.compile_objects(
            staged, policy, [staged.src / f"{c.function}.c" for c in members], version, output
        )
        outcomes: list[tuple[Candidate, list[SymbolNeed] | str]] = []
        for candidate in members:
            if candidate.function in failures:
                reason = failures[candidate.function].strip()
                outcomes.append((candidate, f"submit.data_symbols: VERSION {version}: compile: {reason}"))
                continue
            try:
                needs = data_symbols.needs(staged, candidate.function, version, output / f"{candidate.function}.o")
            except Held as error:
                outcomes.append((candidate, f"submit.data_symbols: VERSION {version}: {error.reason}"))
                continue
            shaped = [
                need
                for need in needs
                if (m := re.fullmatch(r"D_([0-9A-Fa-f]{8})", need.name)) and int(m[1], 16) != need.address
            ]
            if shaped:
                # An address-shaped name is that address; elsewhere it would hide the generated label.
                outcomes.append(
                    (
                        candidate,
                        f"submit.data_symbols: VERSION {version}: {shaped[0].name}: address-shaped name placed at "
                        f"0x{shaped[0].address:08X}; use this version's name or a cross-version identity",
                    )
                )
                continue
            outcomes.append((candidate, needs))
        return outcomes

    refused: dict[str, str] = {}
    proposed: dict[tuple[str, str], dict[int, set[str]]] = {}
    found: dict[str, list[SymbolNeed]] = {}
    with ThreadPoolExecutor(max_workers=policy.cores) as pool:
        for candidate, outcome in (item for chunk in pool.map(compile_chunk, chunks) for item in chunk):
            if isinstance(outcome, str):
                refused.setdefault(candidate.function, outcome)
                continue
            found.setdefault(candidate.function, []).extend(outcome)
            for need in outcome:
                proposed.setdefault((need.version, need.name), {}).setdefault(need.address, set()).add(
                    candidate.function
                )
    for (version, name), addresses in proposed.items():
        # A source already refused elsewhere cannot vote against surviving users.
        active = {address: owners - refused.keys() for address, owners in addresses.items()}
        active = {address: owners for address, owners in active.items() if owners}
        if len(active) <= 1:
            continue
        preferred = data_symbols.preferred_address(staged, version, name)
        if preferred is not None and preferred in active:
            selected = preferred
            evidence = "native data identity and owning ROM pairs"
        else:
            # Every proposal was proved from its owner's ROM. Without an
            # independent identity, retain the largest agreeing group. Equal
            # groups keep the first in source order, never refuse every user.
            selected = max(active, key=lambda address: len(active[address]))
            evidence = "agreeing owning ROM placements"
        for address, owners in active.items():
            if address == selected:
                continue
            for owner in owners:
                refused.setdefault(
                    owner,
                    f"submit.data_symbols: VERSION {version}: {name}: sources disagree on its address; "
                    f"source places 0x{address:08X}, selected 0x{selected:08X} from {evidence}",
                )
    for candidate in candidates:
        if candidate.function in refused:
            receipts.append(f"HELD(submit): {candidate.function}: {refused[candidate.function]}")
    while True:
        pending: list[SymbolNeed] = [need for name, items in found.items() if name not in refused for need in items]
        try:
            edits = data_symbols.resolve(pending, staged, policy) if pending else []
        except Held as error:
            # A refused symbol names its owners; the rest of the batch continues.
            symbol = error.reason.split(":", 1)[0]
            owners = {
                name
                for name, items in found.items()
                if name not in refused
                and any(n.name == symbol or str(staged.version(n.version).symbols) in error.reason for n in items)
            }
            if not owners:
                raise
            for owner in owners:
                refused[owner] = f"submit.data_symbols: {error.reason}"
                receipts.append(f"HELD(submit): {owner}: {refused[owner]}")
            continue
        break
    for candidate in candidates:
        if candidate.function not in refused:
            candidate.symbol_needs = found.get(candidate.function, [])
    for edit in edits:
        split_apply.write(edit.path, edit.after)
    return [candidate for candidate in candidates if candidate.function not in refused]


def _input_culprits(
    staged: Project, candidates: list[Candidate], generations: dict[str, Path], failures: list[str]
) -> dict[str, list[str]]:
    """Attribute symbol-file extraction errors to the sources introducing those rows."""
    faults: dict[str, list[str]] = {}
    for version in failures:
        log = generations[version] / "build.log"
        if not log.is_file():
            continue
        content = log.read_text(errors="replace")
        path = staged.version(version).symbols
        rows = path.read_text().splitlines()
        for diagnostic in content.splitlines():
            if not re.search(r"Duplicate symbol detected|(?i:error|HELD\(|invalid|clashes)", diagnostic):
                continue
            for candidate in candidates:
                for need in candidate.symbol_needs:
                    if need.version != version or not re.search(rf"\b{re.escape(need.name)}\b", diagnostic):
                        continue
                    row = next(
                        (i for i, line in enumerate(rows, 1) if re.match(rf"\s*{re.escape(need.name)}\s*=", line)), None
                    )
                    reason = f"VERSION {version}: data_symbols input {path}:{row or '?'}: {diagnostic.strip()}"
                    if reason not in faults.setdefault(candidate.function, []):
                        faults[candidate.function].append(reason)
    return faults


def _isolate(
    project: Project,
    staged: Project,
    base: _Base,
    policy: Policy,
    candidates: list[Candidate],
    generations: dict[str, Path],
    results: dict[str, build.BuildResult],
    receipts: list[str],
    initial_faults: dict[str, list[str]] | None = None,
    reference: dict[str, Path] | None = None,
) -> tuple[list[Candidate], dict[str, str]]:
    """Name culprits from the built objects, then relink the rest from the same objects."""
    extracted = {version: staged.version(version).split.read_text() for version in generations}
    while True:
        failures = [version for version, result in results.items() if not result.ok]
        reporting.record(
            "proof",
            sources=[candidate.function for candidate in candidates],
            failures={v: staging.compare_failure(staged, v, results[v]) for v in failures},
            generations={v: str(g) for v, g in generations.items()},
        )
        if not failures and not initial_faults:
            return candidates, {v: results[v].sha1_line for v in results}
        names = {candidate.function for candidate in candidates}
        culprits = attribution.diagnose(
            staged, failures, generations, names, (project, reference) if reference is not None else None
        )
        for name, details in _input_culprits(staged, candidates, generations, failures).items():
            culprits.setdefault(name, []).extend(details)
        for name, details in (initial_faults or {}).items():
            culprits.setdefault(name, []).extend(details)
        initial_faults = None
        if not culprits:
            culprits = _bisect(staged, base, policy, candidates, generations, failures, extracted)
        reporting.record("attribution", culprits=culprits)
        for name, details in sorted(culprits.items()):
            receipts.append(f"HELD(match): {name}: build compare failed on " + "; ".join(details[:4]))
        candidates = [candidate for candidate in candidates if candidate.function not in culprits]
        if not candidates:
            return [], {}
        _materialize(staged, base, candidates)
        if not failures:
            return candidates, {v: results[v].sha1_line for v in results}
        if any(not (generations[v] / "obj/src" / f"{c.function}.o").is_file() for c in candidates for v in c.versions):
            # A failed compile chunk discards its siblings; make rebuilds only those, from the cache.
            staging.copy_assembly(project, staged)
            for generation in generations.values():
                staging.independent_objects(generation)
            results = build.build(
                project, policy, list(generations), tree=staged.root, generation_for=generations.__getitem__
            )
            extracted.update({v: staged.version(v).split.read_text() for v in generations})
            continue
        results = _relink(staged, policy, generations, extracted)


def _relink(
    staged: Project, policy: Policy, generations: dict[str, Path], extracted: dict[str, str]
) -> dict[str, build.BuildResult]:
    """Link the staged layout over objects this transaction already built.

    extracted holds the split each generation's link inputs describe. Changes
    confined to owning the same text spans rewrite that graph in place.
    """
    reuse = {}
    for version, generation in generations.items():
        text = staged.version(version).split.read_text()
        reuse[version] = text == extracted[version] or relink.revert_rows(staged, generation, extracted[version], text)
        extracted[version] = text
    reporting.record("relink", reused={v: str(r) for v, r in reuse.items()})
    with ThreadPoolExecutor(max_workers=min(policy.cores, policy.setup_version_jobs, len(generations))) as pool:
        futures = {
            v: pool.submit(
                relink.prove,
                staged,
                policy,
                v,
                g,
                None,
                extracted=reuse[v],
                placed=reuse[v] and relink.place_changed(staged, v, g),
            )
            for v, g in generations.items()
        }
        return {v: future.result() for v, future in futures.items()}


def _bisect(
    staged: Project,
    base: _Base,
    policy: Policy,
    candidates: list[Candidate],
    generations: dict[str, Path],
    failures: list[str],
    extracted: dict[str, str],
) -> dict[str, list[str]]:
    """Collect independent faults and minimal conflicts from retained objects."""
    detail = ", ".join(failures)

    def differs(members: list[Candidate]) -> bool:
        _materialize(staged, base, members)
        return any(not result.ok for result in _relink(staged, policy, generations, extracted).values())

    if differs([]):
        inputs = []
        for version in failures:
            if version not in generations:
                continue
            log = generations[version] / "build.log"
            if log.is_file():
                lines = log.read_text(errors="replace").splitlines()
                diagnostic = next(
                    (
                        (i, line)
                        for i, line in enumerate(lines, 1)
                        if re.search(r"error|HELD\(|Duplicate symbol", line, re.I)
                    ),
                    None,
                )
                if diagnostic is not None:
                    inputs.append(f"input {log}:{diagnostic[0]}: {diagnostic[1][:300]}")
        named = "; " + "; ".join(inputs) if inputs else ""
        held(f"submit.dependencies: cartridge differs on {detail} with no batch sources{named}")
    faults: dict[str, list[str]] = {}

    def inspect(members: list[Candidate]) -> None:
        if not members or not differs(members):
            return
        if len(members) == 1:
            faults[members[0].function] = [f"cartridge differs on {detail}; isolated by relinking retained objects"]
            return
        left, right = members[: len(members) // 2], members[len(members) // 2 :]
        left_bad, right_bad = differs(left), differs(right)
        if left_bad or right_bad:
            if left_bad:
                inspect(left)
            if right_bad:
                inspect(right)
            # A second fault may require members spanning both halves. Check
            # the remainder after removing every independently failing unit.
            remainder = [member for member in members if member.function not in faults]
            inspect(remainder)
            return
        # Each half passes alone: reduce the joint failure to its participants.
        conflict = list(members)
        for member in members:
            reduced = [item for item in conflict if item is not member]
            if differs(reduced):
                conflict = reduced
        for member in conflict:
            faults[member.function] = [
                f"cartridge differs on {detail}; isolated conflicting retained objects: "
                + ", ".join(item.function for item in conflict)
            ]
        inspect([member for member in members if member not in conflict])

    inspect(candidates)
    return faults


def _publish_survivors(
    project: Project,
    staged: Project,
    base: _Base,
    policy: Policy,
    candidates: list[Candidate],
    current: dict[str, Path],
    generations: dict[str, Path],
    started: dict[str, str],
    receipts: list[str],
    sha1: dict[str, str],
) -> tuple[list[Candidate], dict[str, str], list[str]]:
    """Hold attributable report faults, reprove the remainder, then publish it."""
    extracted = {v: staged.version(v).split.read_text() for v in generations}
    while candidates:
        try:
            followups = _commit(project, policy, staged, candidates, current, generations, started)
            return candidates, sha1, followups or []
        except Held as error:
            named = re.match(r"matched function ([A-Za-z_]\w*)(?::| source | linked src object )", error.reason)
            if error.phase != "report" or named is None:
                raise
            owners = {c.function for c in candidates if c.function == named[1]}
            for candidate in candidates:
                for rows in candidate.removed_rows.values():
                    for line in rows:
                        row = split.ROW.fullmatch(line)
                        if row is not None and Path(split.plain(row["path"])).name == named[1]:
                            owners.add(candidate.function)
            if not owners:
                raise
            for name in sorted(owners):
                receipts.append(f"HELD(report): {name}: {error.reason}; batch source was not published")
            candidates = [c for c in candidates if c.function not in owners]
            if not candidates:
                return [], {}, []
            _materialize(staged, base, candidates)
            results = _relink(staged, policy, generations, extracted)
            candidates, sha1 = _isolate(
                project, staged, base, policy, candidates, generations, results, receipts, reference=current
            )
    return [], {}, []


def _commit(
    project: Project,
    policy: Policy,
    staged: Project,
    candidates: list[Candidate],
    current: dict[str, Path],
    generations: dict[str, Path],
    started: dict[str, str],
) -> list[str]:
    """Publish the proved staged inputs and generations together, or nothing."""
    followups: list[str] = []
    unchanged = (
        all(candidate.compiled for candidate in candidates)
        and all((generation / "obj").is_symlink() for generation in generations.values())
        and all(
            hashlib.sha1((current[v] / f"{project.name}.{v}.z64").read_bytes()).hexdigest()
            == project.version(v).baserom_sha1
            for v in project.versions
        )
    )
    reports: dict[str, dict[str, Any]] = {}
    if unchanged:
        for version in project.versions:
            cached = project.root / "versions" / version / "report.json"
            if not cached.is_file():
                reports = {}
                break
            document = progress._json(cached)
            progress._native_counts(document, cached)
            reports[version] = document
    if not reports:
        with ThreadPoolExecutor(
            max_workers=min(policy.cores, policy.setup_version_jobs, len(project.versions))
        ) as pool:
            measured = {
                v: pool.submit(progress.measure, staged, policy, v, generation=generations[v]) for v in project.versions
            }
            reports = {v: future.result() for v, future in measured.items()}
    paths = [staged.root / "config.toml", staged.root / "Makefile", staged.tools / "compiler.sha256"]
    paths += [staged.root / relative for relative in makefile.helpers(config.load(staged.root))]
    paths += [p for v in staged.versions for p in (staged.version(v).split, staged.version(v).symbols)]
    paths += [staged.tools / name.name for name in makefile.TEMPLATES.glob("*.py")] + [staged.tools / "cache.py"]
    paths += [staged.root / "unbake-exclusions.json"]
    from unbake.layout import index

    paths += [index.path(staged), staged.root / "layout.toml", *index.headers(staged)]
    paths += [staged.src / f"{candidate.function}.c" for candidate in candidates]
    writes: dict[Path, bytes] = {}
    for path in paths:
        if not path.is_file():
            continue
        target = project.root / path.relative_to(staged.root)
        content = path.read_bytes()
        if not target.is_file() or target.read_bytes() != content:
            writes[target] = content
    with build.lock(project):
        for version, generation in current.items():
            if build.current_generation(project, version).resolve() != generation.resolve():
                held(f"VERSION {version}: current generation changed during proof")
        latest = staging.fingerprint(project, project.root)
        changed = sorted(key for key in latest.keys() | started.keys() if latest.get(key) != started.get(key))
        if changed:
            held(f"project build inputs changed during proof: {', '.join(changed[:8])}")
        for candidate in candidates:
            if drafts.source_identity(candidate.source.read_bytes()) != candidate.sha256:
                held(f"{candidate.function}: source_sha256 changed during proof")
        ledger = Path(policy.state_root) / project.id / project.workspace_id / "receipts" / "match.jsonl"
        report_paths = {project.root / "versions" / version / "report.json" for version in project.versions}
        touched = set(writes) | {ledger, project.root / "README.md"} | report_paths
        republication = any(candidate.republication for candidate in candidates)
        touched.update(
            project.build / "types" / name for name in ("proven.json", "database.json", "summary.json", "redraft.json")
        )
        touched.update(index.headers(project))
        touched.add(index.path(project))
        before = {path: path.read_bytes() if path.exists() else None for path in touched}
        swapped = []
        try:
            for path, content in writes.items():
                atomic(path, content)
            published_generations = current if unchanged and not writes else generations
            if published_generations is generations:
                for generation in generations.values():
                    staging.independent_objects(generation)
            for version, generation in published_generations.items():
                swap(project.build_link(version), generation)
                swapped.append(version)
            progress.write(project, policy, reports=reports)
            ledger.parent.mkdir(parents=True, exist_ok=True)
            with atomic_files.stream(ledger, "a", encoding="utf-8") as output:
                for candidate in candidates:
                    if not candidate.matched:
                        continue
                    row = {
                        "function": candidate.function,
                        "versions": list(candidate.versions),
                        "sha256": candidate.sha256,
                        "source": str((project.src / f"{candidate.function}.c").relative_to(project.root)),
                        "source_sha256": storage.file_digest(project.src / f"{candidate.function}.c"),
                        "fakematch": list(checks.fakematches(candidate.final)),
                        "compiler_evidence": candidate.compiler,
                        "at": datetime.now(UTC).isoformat(),
                    }
                    output.write(json.dumps(row, sort_keys=True) + "\n")
            with reporting.phase("type_feedback", sources=len(candidates)):
                followups = _feedback(config.load(project.root), policy, candidates, current, strict=republication)
            # Feedback owns shared header writes. Refresh only after its last
            # write so an immediate independent check reuses the proved objects.
            if published_generations is generations:
                staging.publication_stamps(project, generations)
            reporting.record(
                "published",
                sources=[candidate.function for candidate in candidates],
                generations={version: str(generation) for version, generation in published_generations.items()},
            )
        except BaseException:
            for path in index.headers(project) - before.keys():
                path.unlink(missing_ok=True)
            for version in swapped:
                swap(project.build_link(version), current[version])
            for path, previous in before.items():
                if previous is None:
                    path.unlink(missing_ok=True)
                else:
                    atomic(path, previous)
            raise
    return followups


def _type_preflight(
    project: Project, policy: Policy, candidates: list[Candidate], receipts: list[str]
) -> list[Candidate]:
    if all(candidate.compiled for candidate in candidates):
        return candidates
    from unbake.typemap.declarations import validate_sources

    refused = validate_sources(
        project,
        policy,
        [
            (candidate.function, project.src / f"{candidate.function}.c", candidate.versions)
            for candidate in candidates
            if candidate.matched and not candidate.compiled
        ],
    )
    # Declaration acceptance is weaker than the project's actual C compiler
    # (notably GCC 2.8.1's duplicate typedef rules). Compile every surviving
    # folded source on one containing version before preparing the long proof.
    jobs: dict[str, list[Path]] = {}
    for candidate in candidates:
        if candidate.function not in refused and not candidate.compiled:
            jobs.setdefault(candidate.versions[0], []).append(project.src / f"{candidate.function}.c")
    with workspace.temporary(project, prefix="type-preflight-", directory=project.build) as temporary:
        compiled = build.compile_versions(
            project, policy, {v: (sources, Path(temporary) / v) for v, sources in jobs.items()}
        )
    for version, failures in compiled.items():
        for function, diagnostic in failures.items():
            refused[function] = f"VERSION {version}: compile diagnostic: {diagnostic}"
    for function, reason in refused.items():
        receipts.append(f"HELD(types): {function}: {reason}; batch source was not published")
    return [candidate for candidate in candidates if candidate.function not in refused]


def _feedback(
    project: Project,
    policy: Policy,
    candidates: list[Candidate],
    previous: dict[str, Path],
    *,
    strict: bool = False,
) -> list[str]:
    """Feed every proved layout back to solve once for the whole batch."""
    entries = []
    owners_by_version = {version: split.owners_by_alias(project, version) for version in project.versions}
    for candidate in candidates:
        if not candidate.matched:
            continue
        targets = {}
        for version in candidate.versions:
            owners = owners_by_version[version].get(candidate.function, [])
            kind = "src" if candidate.republication else "asm"
            target = previous[version] / "obj" / kind / (owners[0].path + ".o") if owners else None
            if target is not None and target.is_file():
                targets[version] = hashlib.sha256(target.read_bytes()).hexdigest()
        entries.append((candidate.function, project.src / f"{candidate.function}.c", candidate.versions, targets))
    if not entries:
        return []
    try:
        refresh = not all(candidate.compiled for candidate in candidates)
        if refresh:
            type_context.feedback_many(project, entries, policy=policy)
        else:
            type_context.feedback_many(project, entries, policy=policy, regenerate=False)
            from unbake.cli.guidance import command

            followup = command(project.root, "solve")
            return [f"OK(types): refreshed {len(entries)} matched source proofs; follow-up: {followup}"]
    except (Held, OSError, ValueError, RuntimeError) as error:
        if isinstance(error, Held) and error.reason.startswith("types.feedback.source_sha256:"):
            stale = storage.changed_source(project)
            proven = project.build / "types/proven.json"
            records = storage.read(proven, "types.feedback").get("records", {}) if proven.is_file() else {}
            seeded = all(
                records.get(function, {}).get("source_sha256") == storage.file_digest(source)
                for function, source, _, _ in entries
            )
            if (
                seeded
                and stale is not None
                and stale.resolve() not in {source.resolve() for _, source, _, _ in entries}
            ):
                return [f"OK(types): {error.reason}; follow-up: {storage.submit_command(project, stale)}"]
        if strict:
            raise
        reason = error.reason if isinstance(error, Held) else f"types.feedback: {error}"
        return [f"HELD(types): {reason}; batch was published"]
    return []
