"""land(F): fold, prove every holding version against the ROM, write, commit "Match F", push in the background.

An already published unit lands the same way (its row edits are a no-op) and commits "Clean F".

Proof: every ROM piece other than F's row is either a raw ROM slice or an already matched unit, so the ROM of a
version is byte-identical to the original exactly when F's linked .text (strict `n64link place`, every constant
proved) equals the ROM bytes of F's row. That is checked in every holding version before anything is written.
Header text that fold appends is staged first; every published unit that includes a changed header is compiled
against the staged copy (layout.header_step.validate) before the write.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import threading
from collections.abc import Callable
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
    pushed: bool | None = None

    def document(self) -> dict[str, Any]:
        return {"landed": self.landed, "commits": self.commits, "failed": self.failed, "pushed": self.pushed}

    def lines(self) -> list[str]:
        out = [f"landed {name} ({commit[:12]})" for name, commit in zip(self.landed, self.commits, strict=True)]
        out += [f"not landed {name}: {reason}" for name, reason in self.failed.items()]
        if self.pushed is not None:
            out.append("pushed" if self.pushed else "push failed; the commits stay local and the next land retries")
        return out


def _git(project: Project, *args: str, env: dict[str, str] | None = None) -> str:
    result = subprocess.run(["git", *args], cwd=project.root, capture_output=True, text=True, env=env)
    if result.returncode:
        raise Held("land", f"git {args[0]} exited {result.returncode}: {(result.stderr or result.stdout).strip()}")
    return result.stdout


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


def record(project: Project, host: Host) -> str | None:
    """Cycle end: fold the attempt logs into attempts.json and commit it with the reports it moves.

    Return the "Record attempts" commit, or None when no derived file changed.
    """
    from unbake.report import progress

    paths = progress.write(project, host)
    relative = [str(path.relative_to(project.root)) for path in paths]
    if not _git(project, "status", "--porcelain", "--", *relative).strip():
        return None
    _commit(project, host, paths, "Record attempts")
    return _git(project, "rev-parse", "HEAD").strip()


def chosen_compiler(project: Project, function: str) -> str:
    """The compiler of the newest exact attempt; compare records it (compilers.candidates)."""
    exact = [row for row in attempts.read(project, function) if row.exact]
    if not exact:
        raise Held("land", f"land.not_exact: {function}: no exact compare attempt; run compare first")
    return exact[-1].compiler or project.compiler_reference(function)


def _with_compiler(project: Project, function: str, ident: str) -> Project:
    units = {name: value for name, value in project.units.items() if name != function}
    if ident != project.default_compiler:
        units[function] = ident
    return replace(project, units=units)


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
    for version in versions:
        row = compare.row_of(project, function, version)
        obj = runner.compile_unit(view, host, file, version, unit=function)
        with tempfile.TemporaryDirectory(prefix="land-") as temporary:
            work = Path(temporary)
            placed = work / "placed.o"
            runner.place(project, host, obj, version, row, placed, score=False)
            linked = runner.link(project, host, placed, version, row, work)
        if linked != split.words(project, row):
            raise Held("land", f"land.mismatch: {function} {version}: linked bytes differ from the ROM row")
    changed = {
        project.include[-1] / name: text.encode()
        for name, text in headers.items()
        if not (project.include[-1] / name).is_file() or (project.include[-1] / name).read_text() != text
    }
    if changed:
        # A published unit's own source is validated as its new text, the one this land writes.
        header_step.validate(project, host, {**changed, project.src / f"{function}.c": source.encode()})
    return list(versions)


def _row_edits(project: Project, function: str, versions: list[str]) -> list[split.Edit]:
    """asm -> c for F's row in every holding version, path = F."""
    edits = []
    for version in versions:
        row = compare.row_of(project, function, version)
        path = project.version(version).split
        text, lines, segments = split.layout(path)
        lines = list(lines)
        for segment in segments:
            for candidate in segment.rows:
                if candidate.start == row.start and candidate.kind in ("asm", "c"):
                    lines[candidate.line] = split.replace_row(
                        lines[candidate.line], candidate.match, kind="c", path=function
                    )
        after = "".join(lines)
        if after != text:
            edits.append(split.Edit(path, text, after, (version,)))
    return edits


def subject(project: Project, function: str) -> str:
    """The land commit subject, read before the write: "Clean F" for a published unit, else "Match F"."""
    return f"{'Clean' if compare.published(project, function) else 'Match'} {function}"


def land(project: Project, host: Host, file: Path) -> str:
    """Land one exact draft; return the commit id. Nothing is written unless every version proves."""
    from unbake.fold import apply as fold_apply
    from unbake.report import progress

    function = compare.function_of(file)
    message = subject(project, function)
    ident = chosen_compiler(project, function)
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
        if ident != project.default_compiler:
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
        _commit(project, host, sorted({*written, *generated}), message)
    except BaseException:
        for path, previous in written.items():
            if previous is None:
                path.unlink(missing_ok=True)
            else:
                atomic_files.write(path, previous)
        raise
    shutil.rmtree(project.work / function, ignore_errors=True)
    return _git(project, "rev-parse", "HEAD").strip()


def push_commits(project: Project, host: Host) -> bool:
    """git push <remote> HEAD:<branch> with the configured credential; never raises."""
    kind, value = host.credential()
    if kind == "env":
        if value not in os.environ:
            return False
        helper = f'!f() {{ echo username=x-access-token; echo "password=${value}"; }}; f'
    else:
        helper = value
    result = subprocess.run(
        [
            "git",
            "-c",
            "credential.helper=",
            "-c",
            f"credential.helper={helper}",
            "push",
            "-q",
            host.publish_remote,
            f"HEAD:{host.publish_branch}",
        ],
        cwd=project.root,
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


class Pusher:
    """One background push thread. A request during a push makes one more push afterwards (all commits so far)."""

    def __init__(self, project: Project, host: Host, report: Callable[[bool], None]) -> None:
        self.project, self.host, self.report = project, host, report
        self.lock = threading.Lock()
        self.pending = False
        self.thread: threading.Thread | None = None

    def request(self) -> None:
        with self.lock:
            self.pending = True
            if self.thread is None or not self.thread.is_alive():
                self.thread = threading.Thread(target=self._loop, name="push", daemon=True)
                self.thread.start()

    def _loop(self) -> None:
        while True:
            with self.lock:
                if not self.pending:
                    return
                self.pending = False
            self.report(push_commits(self.project, self.host))

    def wait(self) -> None:
        thread = self.thread
        if thread is not None:
            thread.join()


def publish(project: Project, host: Host, files: list[Path], *, push: bool) -> Landed:
    """`unbake publish FILE...`: land each file in turn; one failure does not stop the others."""
    from unbake import config

    result = Landed()
    for file in files:
        current = config.load(project.root)
        try:
            commit = land(current, host, file)
        except Held as error:
            result.failed[file.stem] = error.reason
            continue
        result.landed.append(file.stem)
        result.commits.append(commit)
        steps.ensure(config.load(project.root), host, ["merge-units"])
    if push and result.commits:
        result.pushed = push_commits(project, host)
    return result
