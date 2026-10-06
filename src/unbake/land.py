"""land(F): fold, prove every holding version against the ROM, write, commit "Match F".

An already published unit lands the same way (its row edits are a no-op) and commits "Clean F".

Proof: every ROM piece other than F's row is either a raw ROM slice or an already matched unit, so the ROM of a
version is byte-identical to the original exactly when F's linked .text (strict `n64link place`, every constant
proved) equals the ROM bytes of F's row. That is checked in every holding version before anything is written.
Header text that fold appends is staged first; every published unit that includes a changed header is compiled
against the staged copy (layout.header_step.validate) before the write.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import toml  # type: ignore[import-untyped]

from unbake import atomic as atomic_files
from unbake import buildfiles, runner, steps
from unbake.config import Held, Host, Project
from unbake.layout import split
from unbake.work import attempts, compare


@dataclass
class Landed:
    landed: list[str] = field(default_factory=list)
    commits: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)

    def document(self) -> dict[str, Any]:
        return {"landed": self.landed, "commits": self.commits, "failed": self.failed}

    def lines(self) -> list[str]:
        out = [f"landed {name} ({commit[:12]})" for name, commit in zip(self.landed, self.commits, strict=True)]
        out += [f"not landed {name}: {reason}" for name, reason in self.failed.items()]
        return out


def _git(project: Project, *args: str, env: dict[str, str] | None = None) -> str:
    result = subprocess.run(["git", *args], cwd=project.root, capture_output=True, text=True, env=env)
    if result.returncode:
        raise Held("land", f"git {args[0]} exited {result.returncode}: {(result.stderr or result.stdout).strip()}")
    return result.stdout


def dirty(project: Project) -> set[str]:
    """Paths git reports changed or untracked, relative to the project root."""
    rows = _git(project, "status", "--porcelain", "--untracked-files=all", "-z").split("\0")
    paths = set()
    skip = False
    for row in rows:
        if skip or not row:
            skip = False
            continue
        if row[0] in "RC":
            skip = True  # the next field is the rename's source
        paths.add(row[3:])
    return paths


def commit_generated(project: Project, host: Host, before: set[str], message: str) -> str | None:
    """Commit what the tool changed since BEFORE (the paths dirty when it started); None when nothing did."""
    changed = sorted(dirty(project) - before)
    if not changed:
        return None
    _commit(project, host, [project.root / path for path in changed], message)
    return _git(project, "rev-parse", "HEAD").strip()


def _commit(project: Project, host: Host, paths: list[Path], message: str) -> None:
    _git(project, "add", "--", *(str(path.relative_to(project.root)) for path in paths))
    author = f"{host.publish_author_name} <{host.publish_author_email}>"
    _git(
        project,
        "-c",
        f"user.name={host.publish_author_name}",
        "-c",
        f"user.email={host.publish_author_email}",
        "commit",
        "-q",
        "-m",
        message,
        "--author",
        author,
    )


def record(project: Project, host: Host) -> tuple[str, tuple[str, ...]] | None:
    """Cycle end: fold the attempt logs into attempts.json and commit it with the reports it moves.

    Return the "Record attempts" commit and the functions whose history it records, or None when no attempt
    changed attempts.json (report changes alone are committed with the other generated files)."""
    from unbake.report import progress

    summary = attempts.summary_path(project)
    before = attempts.committed_documents(project)
    paths = progress.write(project, host)
    steps.record(project, "progress", steps.STEPS["progress"].key(project, host))
    after = attempts.committed_documents(project)
    functions = tuple(sorted(name for name in after if after[name] != before.get(name)))
    if not functions:
        return None
    _commit(project, host, sorted({*paths, summary}), "Record attempts: " + ", ".join(functions))
    return _git(project, "rev-parse", "HEAD").strip(), functions


def exact_attempt(project: Project, function: str, file: Path) -> attempts.Attempt:
    """The newest compare of FILE's current bytes, which must be exact; refuse in words that say what to do."""
    from unbake.decomp import checks

    digest = hashlib.sha256(file.read_bytes()).hexdigest()
    found = [row for row in attempts.read(project, function) if row.sha256 == digest]
    if not found:
        raise Held(
            "land",
            f"land.not_compared: {function} was not compared since its last edit. Run: unbake compare {file}",
        )
    attempt = found[-1]
    if attempt.exact:
        return attempt
    version, row = min(attempt.versions.items(), key=lambda item: item[1]["percent"])
    if row["percent"] < 100:
        raise Held(
            "land",
            f"land.not_exact: {function} matches only {attempt.best_percent:.2f}% (lowest version {version}). "
            "Publish needs 100% in every version",
        )
    lines = [checks.plain(finding) for finding in checks.unmarked(file)]
    raise Held("land", f"land.rules: {function} matches 100% but breaks the source rules: {'; '.join(lines)}")


def _with_compiler(project: Project, function: str, ident: str) -> Project:
    units = {name: value for name, value in project.units.items() if name != function}
    if ident != project.default_compiler:
        units[function] = ident
    return replace(project, units=units)


def _builds_row(spec: tuple[Project, Project, Host, str, Path, str]) -> bool:
    """Pool worker: one version's compile, place and link of the staged unit equals its ROM row."""
    project, view, host, function, file, version = spec
    row = compare.row_of(project, function, version)
    obj = runner.compile_unit(view, host, file, version, unit=function)
    with tempfile.TemporaryDirectory(prefix="land-") as temporary:
        work = Path(temporary)
        placed = work / "placed.o"
        runner.place(project, host, obj, version, row, placed, score=False)
        linked = runner.link(project, host, placed, version, row, work, file)
    return linked == split.words(project, row)


def _prove_versions(
    project: Project, host: Host, view: Project, function: str, file: Path, versions: Sequence[str]
) -> None:
    """Build every version at once in the pool; the first version in order that differs refuses."""
    from unbake import pool

    results = pool.run(host, _builds_row, [(project, view, host, function, file, version) for version in versions])
    for version, equal in zip(versions, results, strict=True):
        if not equal:
            raise Held(
                "land",
                f"land.mismatch: {function} compares exact but the {version} ROM built with it differs. "
                f"The tree changed since the compare. Run: unbake compare {project.work / function / f'{function}.c'}",
            )


def prove(project: Project, host: Host, function: str, source: str, headers: dict[str, str], stage: Path) -> list[str]:
    """Compile the folded source against staged headers; its linked bytes must equal the ROM row everywhere."""
    from unbake.layout import header_step

    include = stage / "include"
    for name, text in headers.items():
        atomic_files.text(include / name, text)
    # A staged header's quoted includes ("../types.h") resolve beside it, so the rest of the tree is linked in.
    root = project.include[-1]
    for path in root.rglob("*"):
        mirror = include / path.relative_to(root)
        if path.is_file() and not mirror.exists():
            mirror.parent.mkdir(parents=True, exist_ok=True)
            mirror.symlink_to(path)
    file = stage / "src" / f"{function}.c"
    atomic_files.text(file, source)
    view = replace(project, work_include=(include,))
    versions = split.holding_versions(project, function)
    _prove_versions(project, host, view, function, file, versions)
    changed = {
        project.include[-1] / name: text.encode()
        for name, text in headers.items()
        if not (project.include[-1] / name).is_file() or (project.include[-1] / name).read_text() != text
    }
    if changed:
        # A published unit's own source is validated as its new text, the one this land writes.
        header_step.validate(project, host, {**changed, project.src / f"{function}.c": source.encode()})
    return list(versions)


def _row_edits(project: Project, function: str, versions: list[str], kind: str = "c") -> list[split.Edit]:
    """asm -> KIND (c, or hasm for original asm) for F's row in every holding version, path = F."""
    edits = []
    for version in versions:
        row = compare.row_of(project, function, version)
        path = project.version(version).split
        text, lines, segments = split.layout(path)
        lines = list(lines)
        for segment in segments:
            for candidate in segment.rows:
                if candidate.start == row.start and candidate.kind in ("asm", "c", "hasm"):
                    lines[candidate.line] = split.replace_row(
                        lines[candidate.line], candidate.match, kind=kind, path=function
                    )
        after = "".join(lines)
        if after != text:
            edits.append(split.Edit(path, text, after, (version,)))
    return edits


def subject(project: Project, function: str) -> str:
    """The land commit subject, read before the write: "Clean F" for a published unit, else "Match F"."""
    return f"{'Clean' if compare.published(project, function) else 'Match'} {function}"


def _refuse_edited_headers(project: Project) -> None:
    """Publish places declarations in the shared headers; a hand-edited generated header is dropped by the next
    regeneration, so it is refused before any work."""
    if edited := steps.altered(project, "headers"):
        raise Held(
            "land",
            f"land.generated_edit: generated headers were edited by hand: {', '.join(edited)}. Put the declarations "
            "in the draft; publish places them in the shared headers",
        )


def land(project: Project, host: Host, file: Path) -> str:
    """Land one exact draft; return the commit id. Nothing is written unless every version proves."""
    from unbake.fold import apply as fold_apply
    from unbake.report import progress

    _refuse_edited_headers(project)
    function = compare.function_of(file)
    message = subject(project, function)
    ident = exact_attempt(project, function, file).compiler or project.compiler_reference(function)
    project = _with_compiler(project, function, ident)
    folded = fold_apply.fold(project, host, function, file.read_text())
    headers = {**fold_apply.private_headers(project, function), **folded.headers}
    stage = project.work / "_land" / function
    shutil.rmtree(stage, ignore_errors=True)
    try:
        versions = prove(project, host, function, folded.source, headers, stage)
    finally:
        shutil.rmtree(stage, ignore_errors=True)
    written: dict[Path, bytes | None] = {}

    def put(path: Path, content: bytes) -> None:
        if path not in written:
            written[path] = path.read_bytes() if path.is_file() else None
        atomic_files.write(path, content)

    try:
        put(project.src / f"{function}.c", folded.source.encode())
        for name, text in headers.items():
            target = project.include[-1] / name
            if not target.is_file() or target.read_text() != text:
                put(target, text.encode())
        for edit in [*folded.split_edits, *_row_edits(project, function, versions)]:
            current = Path(edit.path).read_text() if Path(edit.path).is_file() else ""
            put(Path(edit.path), (edit.after if current == edit.before else current).encode())
        config_path = project.root / "config.toml"
        data = toml.loads(config_path.read_text())
        units = dict(data.get("units", {}))
        if ident != project.default_compiler or units.get(function, {}).get("flags"):
            units[function] = {**units.get(function, {}), "compiler": ident}
        else:
            units.pop(function, None)
        if units != data.get("units", {}):
            data["units"] = dict(sorted(units.items()))
            if not units:
                data.pop("units")
            put(config_path, toml.dumps(data).encode())
        from unbake import config

        updated = config.load(project.root)
        generated = buildfiles.write(updated, host)
        steps.record(updated, "buildfiles", buildfiles.input_key(updated, host))
        generated += progress.write(updated, host)
        steps.record(updated, "progress", steps.STEPS["progress"].key(updated, host))
        _commit(project, host, sorted({*written, *generated}), message)
    except BaseException:
        for path, previous in written.items():
            if previous is None:
                path.unlink(missing_ok=True)
            else:
                atomic_files.write(path, previous)
        raise
    shutil.rmtree(project.work / function, ignore_errors=True)
    if headers:
        steps.acknowledge_outputs(project, "headers", [project.include[-1] / name for name in headers])
    return _git(project, "rev-parse", "HEAD").strip()


def land_original(project: Project, host: Host, function: str) -> str:
    """Land one original-asm function as src/F.s; return the commit id. Every holding VERSION must prove the same
    rule from its ROM bytes and give the same .s text, and that text must assemble and link to each ROM row."""
    from unbake import config
    from unbake.decomp import exclusions, original_asm
    from unbake.report import progress

    _refuse_edited_headers(project)
    versions = list(split.holding_versions(project, function))
    rows = [compare.row_of(project, function, version) for version in versions]
    if any(row.kind != "asm" for row in rows):
        raise Held("land", f"land.original_kind: {function}: only an unlanded asm row lands as original asm")
    bodies = [split.words(project, row) for row in rows]
    proofs = [original_asm.prove(project, row, data) for row, data in zip(rows, bodies, strict=True)]
    if len({found.rule for found in proofs}) != 1:
        raise Held("land", f"land.original_versions: {function}: versions prove different rules")
    found = proofs[0]
    texts = {original_asm.write_source(project, host, row, data, found) for row, data in zip(rows, bodies, strict=True)}
    if len(texts) != 1:
        raise Held("land", f"land.original_versions: {function}: versions need different .s text")
    text = texts.pop()
    records = original_asm.load(project)
    records[function] = original_asm.Record(found.rule)
    written: dict[Path, bytes | None] = {}

    def put(path: Path, content: bytes) -> None:
        if path not in written:
            written[path] = path.read_bytes() if path.is_file() else None
        atomic_files.write(path, content)

    try:
        put(project.src / f"{function}.s", text.encode())
        put(project.root / original_asm.MANIFEST, original_asm.dumps(records).encode())
        for edit in [
            *exclusions.publication_edit(project, {function}),
            *_row_edits(project, function, versions, "hasm"),
        ]:
            current = Path(edit.path).read_text() if Path(edit.path).is_file() else ""
            put(Path(edit.path), (edit.after if current == edit.before else current).encode())
        updated = config.load(project.root)
        generated = buildfiles.write(updated, host)
        steps.record(updated, "buildfiles", buildfiles.input_key(updated, host))
        generated += progress.write(updated, host)
        steps.record(updated, "progress", steps.STEPS["progress"].key(updated, host))
        _commit(project, host, sorted({*written, *generated}), f"Original asm {function}")
    except BaseException:
        for path, previous in written.items():
            if previous is None:
                path.unlink(missing_ok=True)
            else:
                atomic_files.write(path, previous)
        raise
    return _git(project, "rev-parse", "HEAD").strip()


def publish(project: Project, host: Host, files: list[Path], *, originals: tuple[str, ...] = ()) -> Landed:
    """`unbake publish FILE... [--original NAME...]`: land each draft file, then each original-asm function, in
    turn; one failure does not stop the others."""
    from unbake import config

    result = Landed()

    def draft(file: Path) -> Callable[[Project], str]:
        return lambda current: land(current, host, file)

    def original(name: str) -> Callable[[Project], str]:
        return lambda current: land_original(current, host, name)

    work = [(file.stem, draft(file)) for file in files] + [(name, original(name)) for name in originals]
    for name, action in work:
        current = config.load(project.root)
        try:
            commit = action(current)
        except Held as error:
            result.failed[name] = error.reason
            continue
        result.landed.append(name)
        result.commits.append(commit)
        steps.ensure(config.load(project.root), host, ["merge-units"])
    return result
