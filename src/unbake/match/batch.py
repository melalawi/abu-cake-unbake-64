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
import shutil
import tempfile
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import toml  # type: ignore[import-untyped]

from unbake.decomp import checks, drafts, symbols_edits, type_context
from unbake.decomp.needs import Need, SymbolNeed
from unbake.layout import split, split_apply
from unbake.layout.header_context import Headers
from unbake.match import attribution, data_symbols, declarations, relink, reporting, staging
from unbake.match.common import atomic, held
from unbake.match.publication import collect, swap
from unbake.project import build, compiler_choice, config, makefile
from unbake.project.config import Held, Policy, Project
from unbake.report import progress

# Rules the fold resolves: they are judged on the folded text, not on admission.
FOLDED_RULES = frozenset({"invented-struct", "local-type-copy", "raw-offset"})


_BISECT_LIMIT = 64


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


def publish(project: Project, policy: Policy, sources: list[Path]) -> list[str]:
    """Admit, fold, prove and publish; refusals are named per source."""
    names = [source.stem for source in sources]
    if len(set(names)) != len(names):
        held("submit.source: duplicate function names in --batch")
    with reporting.session(project), build.lock(project):
        return _publish(config.load(project.root), policy, [source.resolve() for source in sources])


def _publish(project: Project, policy: Policy, sources: list[Path]) -> list[str]:
    receipts = reporting.Receipts()
    started = staging.fingerprint(project, project.root)
    with reporting.phase("admission", sources=len(sources)):
        candidates = []
        for source in sources:
            try:
                candidates.append(_admit(project, policy, source))
            except Held as error:
                receipts.append(f"HELD(submit): {source.stem}: {error.reason}")
    if not candidates:
        return receipts
    project.build.mkdir(parents=True, exist_ok=True)
    workspace = Path(tempfile.mkdtemp(prefix="submit-", dir=project.build))
    try:
        with ExitStack() as holds:
            current: dict[str, Path] = {}
            with build.lock(project):
                for version in project.versions:
                    current[version] = holds.enter_context(build.pin(build.current_generation(project, version)))
            with reporting.phase("stage"):
                tree = workspace / "tree"
                staging.copy_tree(project, project.root, tree, skip=("docs",))
                local_policy = project.tools / "clone-policy.toml"
                if local_policy.is_file():
                    shutil.copy2(local_policy, tree / local_policy.relative_to(project.root))
                staged = staging.project_at(project, tree)
                staging.write_staged(staged, staging.helper_edits(staged))
                base = _Base(staged)
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
            generations: dict[str, Path] = {}
            versions = list(project.versions)
            for version in versions:
                generations[version] = staging.generation(project, version, current[version], holds)
                staging.chunk_stale_sources(generations[version], staged.tools, staged.version(version).symbols)
            with reporting.phase("proof", sources=len(candidates)):
                results = build.build(project, policy, versions, tree=tree, generation_for=generations.__getitem__)
                candidates, sha1 = _isolate(project, staged, base, policy, candidates, generations, results, receipts)
            if not candidates:
                return receipts
            with reporting.phase("publication", sources=len(candidates)):
                _commit(project, policy, staged, candidates, current, generations, started)
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
        collect(project)
        with reporting.phase("type_feedback", sources=len(candidates)):
            receipts.extend(_feedback(config.load(project.root), policy, candidates, current))
        return receipts
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


def _admit(project: Project, policy: Policy, source: Path) -> Candidate:
    function = source.stem
    if source.suffix != ".c" or not re.fullmatch(r"[A-Za-z_]\w*", function):
        held(f"submit.source: {source} must be named <function>.c")
    try:
        content = source.read_bytes()
        text = content.decode("utf-8")
    except (OSError, UnicodeError) as error:
        held(f"submit.source: {source}: {error}")
    versions = split.holding_versions(project, function)
    blockers = [f for f in checks.run(text) if f.fakematch is None and f.rule not in FOLDED_RULES]
    if blockers:
        held("submit.source_rules: " + "; ".join(checks.message(finding) for finding in blockers))
    sha = drafts.source_identity(content)
    rows = [row for row in drafts.Store(policy, project).rows(function) if row["source_sha256"] == sha]
    latest = rows[-1] if rows else None
    destination = project.src / f"{function}.c"
    published = destination.is_file() and not drafts.is_partial(destination.read_text())
    if latest is not None and not latest["identical_everywhere"]:
        from unbake.match.nonmatching import admit

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

    def __init__(self, staged: Project) -> None:
        self.splits = {v: staged.version(v).split.read_text() for v in staged.versions}
        self.config = (staged.root / "config.toml").read_text()
        self.sources = {path.name: path.read_text() for path in staged.src.glob("*.c")}
        exclusions = staged.root / "unbake-exclusions.json"
        self.exclusions = exclusions.read_text() if exclusions.is_file() else None


def _fold(staged: Project, policy: Policy, candidates: list[Candidate], receipts: list[str]) -> list[Candidate]:
    """Fold every source against one context; a refused source leaves the context unchanged."""
    headers = Headers.read(staged)
    base = dict(headers.texts)
    accepted = []
    for candidate in candidates:
        try:
            folded = declarations.fold_source(
                staged,
                policy,
                headers,
                candidate.function,
                candidate.content.decode("utf-8"),
                candidate.versions,
                prove_headers=False,
            )
            blockers = [f for f in checks.run(folded.source) if f.fakematch is None]
            if blockers:
                held("submit.source_rules: " + "; ".join(checks.message(finding) for finding in blockers))
            headers.apply(folded.headers)
        except Held as error:
            receipts.append(f"HELD(submit): {candidate.function}: submit.fold: {error.reason}")
            continue
        candidate.final = folded.source
        candidate.removed_rows = folded.removed_rows
        accepted.append(candidate)
    for path, text in headers.texts.items():
        if base.get(path) != text:
            split_apply.write(path, text)
    return accepted


def _materialize(staged: Project, base: _Base, candidates: list[Candidate]) -> None:
    """Render sources, rows, compiler units and exclusions for exactly these candidates."""
    selected = {candidate.function: candidate for candidate in candidates}
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
    chosen = [c.compiler for c in candidates if c.matched and c.compiler.get("exact_candidates")]
    config_text = toml.dumps(compiler_choice.fold_units(toml.loads(base.config), chosen)) if chosen else base.config
    if (staged.root / "config.toml").read_text() != config_text:
        split_apply.write(staged.root / "config.toml", config_text)
    _recipe(staged)
    exclusions = staged.root / "unbake-exclusions.json"
    if base.exclusions is not None:
        split_apply.write(exclusions, base.exclusions)
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
            if len(own) != 1 or split.ROW.fullmatch(own[0])["kind"] != "asm":  # type: ignore[index]
                refused.setdefault(candidate.function, f"submit.row: VERSION {version} requires one asm row")
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
            if row["kind"] != "asm" or name in seen:
                held(f"{name}: VERSION {version} requires one asm row")
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


def _data_symbols(staged: Project, policy: Policy, candidates: list[Candidate], receipts: list[str]) -> list[Candidate]:
    """Place unknown data a source declares from its owning ROM relocations, in parallel."""
    known = {v: set(split.symbols(staged.version(v).symbols)[1]) for v in staged.versions}
    work = staged.work / "data-symbols"
    work.mkdir(parents=True, exist_ok=True)
    jobs: list[tuple[Candidate, str]] = []
    for candidate in candidates:
        if not candidate.matched:
            continue
        declared = set(re.findall(r"\bextern\b[^;()]*?\b([A-Za-z_]\w*)\s*(?:\[[^\]]*\]\s*)*;", candidate.final))
        jobs.extend(
            (candidate, version) for version in candidate.versions if declared - known[version] - {candidate.function}
        )

    # One interpreter per chunk compiles through the content cache the proof build reuses.
    chunks: list[tuple[str, list[Candidate]]] = []
    for version in staged.versions:
        members = [candidate for candidate, v in jobs if v == version]
        size = max(1, -(-len(members) // policy.cores))
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
                reason = failures[candidate.function].splitlines()[0][:300]
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
        if len(addresses) > 1:
            for owners in addresses.values():
                for owner in owners:
                    refused.setdefault(
                        owner, f"submit.data_symbols: VERSION {version}: {name}: sources disagree on its address"
                    )
    for candidate in candidates:
        if candidate.function in refused:
            receipts.append(f"HELD(submit): {candidate.function}: {refused[candidate.function]}")
    while True:
        pending: list[Need] = [need for name, items in found.items() if name not in refused for need in items]
        try:
            edits = symbols_edits.resolve(pending, staged, policy) if pending else []
        except Held as error:
            # A refused symbol names its owners; the rest of the batch continues.
            symbol = error.reason.split(":", 1)[0]
            owners = {
                name for name, items in found.items() if name not in refused and any(n.name == symbol for n in items)
            }
            if not owners:
                raise
            for owner in owners:
                refused[owner] = f"submit.data_symbols: {error.reason}"
                receipts.append(f"HELD(submit): {owner}: {refused[owner]}")
            continue
        break
    for edit in edits:
        split_apply.write(edit.path, edit.after)
    return [candidate for candidate in candidates if candidate.function not in refused]


def _isolate(
    project: Project,
    staged: Project,
    base: _Base,
    policy: Policy,
    candidates: list[Candidate],
    generations: dict[str, Path],
    results: dict[str, build.BuildResult],
    receipts: list[str],
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
        if not failures:
            return candidates, {v: results[v].sha1_line for v in results}
        names = {candidate.function for candidate in candidates}
        culprits = attribution.diagnose(staged, failures, generations, names)
        reporting.record("attribution", culprits=culprits)
        if not culprits and len(candidates) > _BISECT_LIMIT:
            # Halving thousands of sources costs a link per step and names one culprit.
            detail = "; ".join(staging.compare_failure(staged, v, results[v])[-300:] for v in failures)
            held(f"submit.attribution: unattributed proof failure across {len(candidates)} sources: {detail}")
        if not culprits:
            culprits = _bisect(staged, base, policy, candidates, generations, failures, extracted)
        for name, details in sorted(culprits.items()):
            receipts.append(f"HELD(match): {name}: build compare failed on " + "; ".join(details[:4]))
        candidates = [candidate for candidate in candidates if candidate.function not in culprits]
        if not candidates:
            return [], {}
        _materialize(staged, base, candidates)
        if any(not (generations[v] / "obj/src" / f"{c.function}.o").is_file() for c in candidates for v in c.versions):
            # A failed compile chunk discards its siblings; make rebuilds only those, from the cache.
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

    extracted holds the split each generation's link inputs describe; a split that
    only returned C rows to assembly is rewritten in place instead of re-extracted.
    """
    reuse = {}
    for version, generation in generations.items():
        text = staged.version(version).split.read_text()
        reuse[version] = text == extracted[version] or relink.revert_rows(staged, generation, extracted[version], text)
        extracted[version] = text
    reporting.record("relink", reused={v: str(r) for v, r in reuse.items()})
    with ThreadPoolExecutor(max_workers=len(generations)) as pool:
        futures = {
            v: pool.submit(relink.prove, staged, policy, v, g, None, extracted=reuse[v]) for v, g in generations.items()
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
    """Find one unattributed fault by relinking halves of the retained objects."""
    detail = ", ".join(failures)
    suspect, accepted = list(candidates), list[Candidate]()
    while len(suspect) > 1:
        left, right = suspect[: len(suspect) // 2], suspect[len(suspect) // 2 :]
        _materialize(staged, base, accepted + left)
        if all(result.ok for result in _relink(staged, policy, generations, extracted).values()):
            accepted += left
            suspect = right
        else:
            suspect = left
    return {suspect[0].function: [f"cartridge differs on {detail}; isolated by relinking retained objects"]}


def _commit(
    project: Project,
    policy: Policy,
    staged: Project,
    candidates: list[Candidate],
    current: dict[str, Path],
    generations: dict[str, Path],
    started: dict[str, str],
) -> None:
    """Publish the proved staged inputs and generations together, or nothing."""
    reports = {v: progress.measure(staged, policy, v, generation=generations[v]) for v in project.versions}
    paths = [staged.root / "config.toml", staged.tools / "build.json", staged.tools / "compiler.sha256"]
    paths += [p for v in staged.versions for p in (staged.version(v).split, staged.version(v).symbols)]
    paths += [staged.tools / name.name for name in makefile.TEMPLATES.glob("*.py")] + [staged.tools / "cache.py"]
    paths += [staged.root / "unbake-exclusions.json"]
    paths += [path for root in staged.include for path in root.rglob("*.h")]
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
        before = {path: path.read_bytes() if path.exists() else None for path in touched}
        swapped = []
        try:
            for path, content in writes.items():
                atomic(path, content)
            for version, generation in generations.items():
                swap(project.build_link(version), generation)
                swapped.append(version)
            progress.write(project, policy, reports=reports)
            ledger.parent.mkdir(parents=True, exist_ok=True)
            with ledger.open("a", encoding="utf-8") as output:
                for candidate in candidates:
                    if not candidate.matched:
                        continue
                    row = {
                        "function": candidate.function,
                        "versions": list(candidate.versions),
                        "sha256": candidate.sha256,
                        "fakematch": list(checks.fakematches(candidate.final)),
                        "compiler_evidence": candidate.compiler,
                        "at": datetime.now(UTC).isoformat(),
                    }
                    output.write(json.dumps(row, sort_keys=True) + "\n")
            reporting.record(
                "published",
                sources=[candidate.function for candidate in candidates],
                generations={version: str(generation) for version, generation in generations.items()},
            )
        except BaseException:
            for version in swapped:
                swap(project.build_link(version), current[version])
            for path, previous in before.items():
                if previous is None:
                    path.unlink(missing_ok=True)
                else:
                    atomic(path, previous)
            raise


def _feedback(project: Project, policy: Policy, candidates: list[Candidate], previous: dict[str, Path]) -> list[str]:
    """Feed every proved layout back to solve once for the whole batch."""
    entries = []
    for candidate in candidates:
        if not candidate.matched:
            continue
        targets = {}
        for version in candidate.versions:
            owners = split.owners_by_alias(project, version).get(candidate.function, [])
            target = previous[version] / "obj" / "asm" / (owners[0].path + ".o") if owners else None
            if target is not None and target.is_file():
                targets[version] = hashlib.sha256(target.read_bytes()).hexdigest()
        entries.append((candidate.function, project.src / f"{candidate.function}.c", candidate.versions, targets))
    if not entries:
        return []
    try:
        type_context.feedback_many(project, entries, policy=policy)
    except (Held, OSError, ValueError, RuntimeError) as error:
        reason = error.reason if isinstance(error, Held) else f"types.feedback: {error}"
        return [f"HELD(types): {reason}; batch was published"]
    return []
