"""Publish changed functions after comparing existing native linked bytes with ROM."""

from __future__ import annotations

import time
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, Any

from unbake import atomic, buildfiles, config, journal, process
from unbake.cache import Cache
from unbake.config import Held, Host, Project
from unbake.layout import split
from unbake.process import capture
from unbake.process import named as cause_named
from unbake.work import attempts

if TYPE_CHECKING:
    from unbake.compilers.drivers import Tools


def _git(project: Project, *args: str) -> str:
    argv, stdin = process.git_pathspec(["git", *args])
    return process.run_tool(argv, project.root, "publish", stdin=stdin).strip()


@attempts.with_ledger
def admission(
    project: Project,
    host: Host,
    head: str,
    base: str,
    *,
    producer_sources: tuple[Path, ...] = (),
    producer_tools: Tools | None = None,
    producer_receipts: tuple[dict[str, Any], ...] = (),
) -> dict[str, Any]:
    """Changed native extents and hygiene; explicit owner sources reuse this same admission.

    producer_sources selects prepared existing DATA/resources for an owner-requested
    capture without scanning unrelated history or building/replaying native work.
    """
    started = time.perf_counter_ns()
    if _git(project, "status", "--porcelain"):
        raise Held(
            cause_named(
                "publish.push_dirty",
                "Commit owned edits before admission",
                owner="project.publication_push",
                stage="publish",
            )
        )
    changed = set(_git(project, "diff", "--name-only", "-z", base, head).split("\0")) - {""}
    sources = tuple(
        sorted(
            {
                project.root / name
                for name in changed
                if (project.root / name).parent == project.src and Path(name).suffix in (".c", ".s")
            }
            | set(producer_sources)
        )
    )
    from unbake.report import data

    # Admission follows authored changes, never an all-producer history sweep.
    # Explicit owner recovery uses record_input_projections/record_producers.
    proofs = {}
    previous_project = (
        config.load(project.root, text=_git(project, "show", base + ":config.toml"))
        if "config.toml" in changed
        else project
    )
    previous_recipes = _git(project, "show", base + ":units.mk") if "units.mk" in changed else None
    changed_paths = {project.root / name for name in changed}
    changed_headers = {path for path in changed_paths if path.suffix in {".h", ".inc"}}
    graph = None
    symbol_versions: dict[str, tuple[dict[str, int] | None, dict[str, int] | None]] = {}

    def relevant_inputs(version: str, unit: Any, source: Path) -> bool:
        nonlocal graph
        if "config.toml" in changed or previous_recipes is not None:
            # Binding changes are selected by the layout comparison below;
            # this projection needs only the unit's compiler/flag identity.
            binding = {"unit": {"name": unit.name, "kind": unit.kind}}
            current_config = data.producer_configuration(project, version, binding)
            if version not in previous_project.versions:
                return True
            before_config = data.producer_configuration(previous_project, version, binding)
            if previous_recipes is not None:
                before_config["unit_recipe"] = [
                    line
                    for line in previous_recipes.splitlines()
                    if "src/" + unit.name + "." in line or "data/" + unit.name + "." in line
                ]
            if data.canonical_configuration(current_config) != data.canonical_configuration(before_config):
                return True
        if "tools/compilers.sha256" in changed:
            return True
        script = f"versions/{version}/" + (
            project.name + ".data.ld" if unit.kind == "data" else "resources/" + unit.name + ".ld"
        )
        if isinstance(unit, buildfiles.MixedData):
            script = f"versions/{version}/data/{unit.name}.ld"
            if "tools/report-verifier.zip" in changed:
                return True
        if script in changed:
            return True
        symbol_paths = {
            project.version(version).symbols.relative_to(project.root).as_posix(),
            f"versions/{version}/symbols.ld",
        } & changed
        if not changed_headers and not symbol_paths:
            return False
        if unit.kind == "data":
            from unbake.compilers import drivers
            from unbake.project.headers import Graph, scan

            if graph is None:
                graph = Graph.capture(project)
            closure = graph.closure((source,), drivers.flags(project, version, unit.name))
            paths = {source, *closure.paths}
            if any(include.unknown for path in paths for include in scan(graph.read(path).decode())):
                return True
            # Missing include probes are relevant when a commit removes/adds a header.
            paths.update(project.root.joinpath(*pin.path.parts) for pin in closure.dependency_set.files)
        else:
            paths = {source, *buildfiles.resource_inputs(project)}
            paths.update(path for path in changed_headers if path.is_relative_to(project.root / "resources"))
        if paths & changed_headers:
            return True
        if symbol_paths:
            # Only changed symbol definitions spelled by this producer's inputs
            # select it; unrelated generated linker additions do not select DATA.
            from unbake.compilers import drivers

            references = data.linker_references(
                source,
                [*drivers.flags(project, version, unit.name), *project.cppflags],
                {path: path.read_text() for path in paths if path.is_file()},
            )
            if references is None:
                return True
            for name in symbol_paths:
                path = project.root / name
                if name not in symbol_versions:
                    symbol_versions[name] = (
                        data.linker_symbols(path, _git(project, "show", base + ":" + name)),
                        data.linker_symbols(path, path.read_text()),
                    )
                before, after = symbol_versions[name]
                if before is None or after is None:
                    return True
                if data.referenced_symbols(references, before) != data.referenced_symbols(references, after):
                    return True
        return False

    data_scopes = []
    resource_scopes = []
    selected_sources = set(sources)
    for version in project.versions:
        meta = project.version(version)
        current = buildfiles.data_bindings(project, version)
        split_name = meta.split.relative_to(project.root).as_posix()
        prior_text = _git(project, "show", base + ":" + split_name) if split_name in changed else None
        previous = buildfiles.data_bindings(project, version, text=prior_text) if prior_text is not None else current
        for unit, binding in current.items():
            source = project.src / (unit.name + ".c")
            if source in sources or previous.get(unit) != binding or relevant_inputs(version, unit, source):
                if not source.is_file():
                    raise Held(
                        cause_named(
                            "publish.source_missing",
                            f"Declared DATA source missing: {source.name}",
                            owner="project.publication_push",
                            stage="publish",
                        )
                    )
                data_scopes.append((version, unit))
                selected_sources.add(source)
        current_resources = buildfiles.resource_bindings(project, version)
        previous_resources = (
            buildfiles.resource_bindings(project, version, text=prior_text)
            if prior_text is not None
            else current_resources
        )
        for resource, source_name in current_resources.items():
            source = project.root / source_name
            resource_headers = {p.relative_to(project.root).as_posix() for p in buildfiles.resource_inputs(project)}
            if (
                source in sources
                or source_name in changed
                or resource not in previous_resources
                or changed & resource_headers
                or relevant_inputs(version, resource, source)
            ):
                resource_scopes.append((version, resource))
                selected_sources.add(source)
    from unbake.decomp import checks

    findings = checks.findings(project, tuple(sorted(selected_sources)), Cache(project.cache))
    if findings.rows:
        raise Held(
            cause_named(
                "publish.push_rules",
                checks.plain(findings.rows[0].finding),
                owner="project.publication_push",
                stage="publish",
            )
        )
    for _, resource in resource_scopes:
        raw = checks.resource_opcodes(project.root / resource.source)
        if raw:
            raise Held(
                cause_named(
                    "publish.push_rules",
                    f"{resource.source}: symbolic resource instructions required: {raw[0].text}",
                    owner="project.publication_push",
                    stage="publish",
                )
            )
    scopes = []
    work = {"native_bytes_read": 0, "rom_bytes_read": 0, "functions_compared": 0}
    for version in project.versions:
        for row in split.functions(project, version):
            if row.kind not in ("c", "hasm"):
                continue
            code_unit = Path(row.path).name
            source = project.src / (code_unit + (".s" if row.kind == "hasm" else ".c"))
            if source not in sources:
                continue
            if not source.is_file():
                raise Held(
                    cause_named(
                        "publish.source_missing",
                        f"Changed source missing: {source.name}",
                        owner="project.publication_push",
                        stage="publish",
                    )
                )
            native = project.build_link(version) / ("hasm" if row.kind == "hasm" else "units") / (code_unit + ".bin")
            if not native.is_file():
                target = native.relative_to(project.root).as_posix()
                raise Held(
                    cause_named(
                        "publish.native_missing",
                        f"Native linked output missing; prepare: make -j12 {target}",
                        owner="project.publication_push",
                        stage="publish",
                    )
                )
            linked = native.read_bytes()
            original = split.words(project, row)
            if linked != original:
                raise Held(
                    cause_named(
                        "publish.native_mismatch",
                        f"{code_unit} {version}: native linked bytes differ from ROM slice",
                        owner="project.publication_push",
                        stage="publish",
                    )
                )
            scopes.append({"function": code_unit, "version": version, "bytes": len(linked)})
            work["native_bytes_read"] += len(linked)
            work["rom_bytes_read"] += len(original)
            work["functions_compared"] += 1
    for version, unit in data_scopes:
        native = project.build_link(version) / "data" / (unit.name + ".bin")
        if not native.is_file():
            target = native.relative_to(project.root).as_posix()
            raise Held(
                cause_named(
                    "publish.native_missing",
                    f"Native linked output missing; prepare: make -j12 {target}",
                    owner="project.publication_push",
                    stage="publish",
                )
            )
        linked = native.read_bytes()
        with project.version(version).baserom.open("rb") as stream:
            stream.seek(unit.start)
            original = stream.read(unit.size)
        if len(original) != unit.size or linked != original:
            raise Held(
                cause_named(
                    "publish.native_mismatch",
                    f"{unit.name} {version}: native DATA bytes differ from ROM extent",
                    owner="project.publication_push",
                    stage="publish",
                )
            )
        receipt = next(
            (
                r
                for r in producer_receipts
                if {"data": unit.name, "version": version, "rom_start": unit.start, "bytes": unit.size}
                in r.get("check", {}).get("scopes", [])
            ),
            None,
        )
        proof = data.capture_producer(
            project,
            version,
            unit,
            linked,
            original,
            host=host,
            producer_tools=producer_tools,
            publication_receipt=receipt,
        )
        if proof is not None:
            proofs[version, unit] = proof
        scopes.append({"data": unit.name, "version": version, "rom_start": unit.start, "bytes": unit.size})
        work["native_bytes_read"] += len(linked)
        work["rom_bytes_read"] += len(original)
    if data_scopes:
        work["data_extents_compared"] = len(data_scopes)
    for version, resource in resource_scopes:
        native = project.build_link(version) / "resources" / (resource.name + ".bin")
        if not native.is_file():
            raise Held(
                cause_named(
                    "publish.native_missing",
                    f"Native resource output missing; prepare: make -j12 {native.relative_to(project.root).as_posix()}",
                    owner="project.publication_push",
                    stage="publish",
                )
            )
        linked = native.read_bytes()
        with project.version(version).baserom.open("rb") as stream:
            stream.seek(resource.start)
            original = stream.read(resource.size)
        if len(original) != resource.size or linked != original:
            raise Held(
                cause_named(
                    "publish.native_mismatch",
                    f"{resource.name} {version}: native resource bytes differ from ROM extent",
                    owner="project.publication_push",
                    stage="publish",
                )
            )
        proof = data.capture_producer(project, version, resource, linked, original)
        if proof is not None:
            proofs[version, resource] = proof
        scopes.append(
            {
                "resource": resource.name,
                "version": version,
                "rom_start": resource.start,
                "bytes": resource.size,
                "execution_vma": resource.execution_address,
                "resident_lma": resource.address,
            }
        )
        work["native_bytes_read"] += len(linked)
        work["rom_bytes_read"] += len(original)
    if resource_scopes:
        work["resource_extents_compared"] = len(resource_scopes)
    return {
        "ok": True,
        "head": head,
        "base": base,
        "scopes": scopes,
        "source_findings": [],
        "native_data": list(proofs.values()),
        "work": work,
        "elapsed_ns": time.perf_counter_ns() - started,
    }


def resolve_conflicts(project: Project, host: Host) -> bool:
    """Regenerate build projections and merge owned reports during the existing rebase.

    Authored/source conflicts retain Git's refusal. README is eligible only if
    both branches agree outside the owning progress section.
    """
    from unbake import buildfiles
    from unbake.report import progress, readme_layout, verify

    conflicts = tuple(p for p in _git(project, "diff", "--name-only", "--diff-filter=U", "-z").split("\0") if p)
    build_generated = {
        "Makefile",
        "units.mk",
        *("versions/" + v + "/slices.mk" for v in project.versions),
        *("versions/" + v + "/symbols.ld" for v in project.versions),
    }
    generated = {verify.BUNDLE, ".github/workflows/progress.yml", ".gitlab-ci.yml"}
    allowed = {
        attempts.PATH,
        verify.MANIFEST,
        "README.md",
        *build_generated,
        *generated,
        *("versions/" + v + "/report.json" for v in project.versions),
    }
    if not conflicts or set(conflicts) - allowed:
        return False
    with journal.transaction(project):
        if attempts.PATH in conflicts:
            sides = [_git(project, "show", ":" + str(stage) + ":" + attempts.PATH).encode() for stage in (1, 2, 3)]
            merged = attempts.Ledger.merge(*sides, project=project)
            atomic.write(project.root / attempts.PATH, merged)
            # Strict readback owns both project identity and current-source projection.
            history = attempts.ledger(project)
            history._refresh()
            expected = {row["event_id"] for row in attempts.read_records(merged, project)}
            if set(history.events) != expected:
                raise Held(
                    cause_named(
                        "ledger.merge_readback",
                        "concurrent event union differs on readback",
                        owner="work.attempts",
                        stage="publish",
                    )
                )
        if "README.md" in conflicts:
            ours, theirs = (_git(project, "show", ":" + str(stage) + ":README.md") for stage in (2, 3))
            left, _, right = readme_layout.section(ours)
            other_left, _, other_right = readme_layout.section(theirs)
            if (left, right) != (other_left, other_right):
                raise Held(
                    cause_named(
                        "publish.readme_conflict",
                        "authored README sections conflict; preserve both branches",
                        owner="project.publication_push",
                        stage="publish",
                    )
                )
            atomic.text(project.root / "README.md", ours)
        if verify.MANIFEST in conflicts:
            # The old report manifest is a projection, never history authority.
            atomic.text(project.root / verify.MANIFEST, _git(project, "show", ":2:" + verify.MANIFEST))
        for name in conflicts:
            if name.startswith("versions/") and name.endswith("/report.json"):
                atomic.text(project.root / name, _git(project, "show", ":2:" + name))
        current = config.load(project.root)
        written = buildfiles.write(current, host) if set(conflicts) & build_generated else []
        if set(conflicts) - build_generated:
            written.extend(buildfiles.write_progress(current, publish_branch=host.publish_branch))
            written.extend(progress.write(current, host, source_only=True))
        written.extend(attempts.storage_paths(current))
        _git(project, "add", "--", *sorted({*conflicts, *(p.relative_to(project.root).as_posix() for p in written)}))
    return True


def rebase(project: Project, host: Host) -> None:
    try:
        _git(project, "rebase", "FETCH_HEAD")
        return
    except Held as error:
        current = error
    while resolve_conflicts(project, host):
        try:
            _git(project, "-c", "core.editor=true", "rebase", "--continue")
            return
        except Held as error:
            current = error
            if not _git(project, "diff", "--name-only", "--diff-filter=U", "-z"):
                raise
    conflicts = tuple(p for p in _git(project, "diff", "--name-only", "--diff-filter=U", "-z").split("\0") if p)
    raise Held(
        current.fault.framed(
            "project.publication_push",
            "publish",
            "publication rebase stopped with unresolved conflicts",
            {"conflicts": list(conflicts)},
        ),
        data={**current.data, "conflicts": list(conflicts)},
    ) from current


@attempts.with_ledger
def push(project: Project, host: Host, remote: str, *, attempts_limit: int = 5) -> dict[str, Any]:
    """Fetch, rebase when needed, reprove affected scopes, and push without force.

    The existing native CAS supplies current qualified linked extents. Every
    changed commit repeats admission; missing proof refuses without native work.
    """
    if not remote or remote.startswith("-"):
        raise Held(
            cause_named(
                "publish.push_remote",
                "publish.push_remote: supply a remote name or URL",
                owner="project.publication_push",
                stage="publish",
            )
        )
    started = time.perf_counter_ns()
    branch = host.publish_branch
    _git(project, "check-ref-format", "refs/heads/" + branch)
    original = _git(project, "rev-parse", "HEAD")
    validated = None
    checked: dict[str, Any] = {}
    for _ in range(attempts_limit):
        _git(project, "fetch", "--", remote, branch)
        tip = _git(project, "rev-parse", "FETCH_HEAD")
        base = _git(project, "merge-base", "HEAD", "FETCH_HEAD")
        if base != tip:
            if _git(project, "status", "--porcelain", "--untracked-files=no"):
                raise Held(
                    cause_named(
                        "publish.push_dirty",
                        "publish.push_dirty: commit tracked edits before rebasing publication",
                        owner="project.publication_push",
                        stage="publish",
                    )
                )
            try:
                rebase(project, host)
            except KeyboardInterrupt:
                with suppress(Held):
                    _git(project, "rebase", "--abort")
                raise
            except Held as error:
                _git(project, "rebase", "--abort")
                raise Held(
                    capture(
                        error,
                        cause=cause_named(
                            "publish.push_conflict",
                            "publish.push_conflict: concurrent changes conflict; local commits retained",
                            owner="project.publication_push",
                            stage="publish",
                        ),
                    ),
                    data=error.data,
                ) from error
        current = config.load(project.root)
        head = _git(project, "rev-parse", "HEAD")
        if _git(project, "status", "--porcelain", "--untracked-files=no"):
            raise Held(
                cause_named(
                    "publish.push_dirty",
                    "commit tracked edits before the final check",
                    owner="project.publication_push",
                    stage="publish",
                )
            )
        if validated != (head, tip):
            untracked = _git(project, "ls-files", "--others", "--exclude-standard", "-z")
            checked = admission(current, host, head, tip)
            if (
                not isinstance(checked, dict)
                or checked.get("ok") is not True
                or checked.get("head") != head
                or checked.get("base") != tip
                or not isinstance(checked.get("scopes"), list)
            ):
                raise Held(
                    cause_named(
                        "publish.check_failed",
                        "Canonical admission did not pass for the final SHA",
                        owner="project.publication_push",
                        stage="publish",
                    ),
                    data={"check": checked, "head": head},
                )
            if (
                _git(project, "rev-parse", "HEAD") != head
                or _git(project, "status", "--porcelain", "--untracked-files=no")
                or _git(project, "ls-files", "--others", "--exclude-standard", "-z") != untracked
            ):
                raise Held(
                    cause_named(
                        "publish.check_changed",
                        "HEAD or owned files changed during the final check; nothing pushed",
                        owner="project.publication_push",
                        stage="publish",
                    ),
                    data={"check": checked, "head": head},
                )
            # Persist the already accepted standalone proofs in the existing tracked
            # Ledger. CI owns canonical report regeneration; no verifier runs here.
            from unbake.report import data

            with journal.transaction(current):
                events = data.record_producers(current, checked.pop("native_data", []))
                if events:
                    # The first packed history commit must carry its canonical reader.
                    # Generation is source-only; admission remains native bytes + hygiene.
                    readers = buildfiles.write_progress(current, publish_branch=host.publish_branch)
                    _git(
                        current,
                        "add",
                        "--",
                        attempts.PATH,
                        *(str(p.relative_to(current.root)) for p in [*attempts.storage_paths(current), *readers]),
                    )
                    _git(current, "commit", "-m", "Record accepted standalone native DATA and resource proofs")
                    head = _git(current, "rev-parse", "HEAD")
                    checked["head"] = head
                    checked["native_data_events"] = events
            validated = (head, tip)
        try:
            _git(project, "push", "--", remote, head + ":refs/heads/" + branch)
        except Held as error:
            # A transport/authentication refusal is not a concurrent publication.
            # Only a newly fetched remote tip authorizes another reconciliation.
            _git(project, "fetch", "--", remote, branch)
            if _git(project, "rev-parse", "FETCH_HEAD") == tip:
                raise Held(
                    capture(
                        error,
                        cause=cause_named(
                            "publish.push_transport",
                            "publish.push_transport: remote refused push; local commits retained",
                            owner="project.publication_push",
                            stage="publish",
                        ),
                    )
                ) from error
            continue
        return {
            "remote": remote,
            "branch": branch,
            "before": original,
            "head": head,
            "check": checked,
            "elapsed_ns": time.perf_counter_ns() - started,
            "reconciled": checked.get("affected_consumers", []),
        }
    raise Held(
        cause_named(
            "publish.push_race",
            "publish.push_race: remote moved repeatedly; local commits retained, retry publish --push",
            owner="project.publication_push",
            stage="publish",
        )
    )
