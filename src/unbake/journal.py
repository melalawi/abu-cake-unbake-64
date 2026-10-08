"""All-or-nothing changes to many project files: a journal of the bytes each path had before.

`with Journal(directory) as journal:` then `journal.save(paths)` before writing them. On any exception the saved
paths get their old bytes back (or are removed when they did not exist). A process killed mid-change leaves the
journal, and `recover(directory)` restores the same way; every Journal recovers before it starts. On success
the journal is discarded.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import stat
import uuid
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from pathlib import Path
from types import TracebackType
from typing import Any, TypeVar, cast

from unbake import atomic as atomic_files
from unbake import inputs, process, strict_json
from unbake.config import Held
from unbake.process import named

INDEX = "index.json"
_current: ContextVar[Journal | None] = ContextVar("journal.current", default=None)


def refuse(reason: str) -> None:
    raise Held(named("journal.contract", reason, owner="journal", stage="recover"))


def image(path: Path) -> dict[str, str] | None:
    if not path.is_file():
        return None
    sha = hashlib.sha256()
    blob = hashlib.sha1(b"blob " + str(path.stat().st_size).encode() + b"\0")
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            sha.update(chunk)
            blob.update(chunk)
    return {
        "sha256": sha.hexdigest(),
        "blob": blob.hexdigest(),
        "mode": "100755" if path.stat().st_mode & 0o111 else "100644",
    }


def _prior_archive(directory: Path, document: dict[str, Any]) -> Path | None:
    """An existing operation archive must describe the same outputs and a safe state transition."""
    target: Path = directory.with_name(directory.name + ".archive") / document["operation_id"]
    if not target.exists():
        return None
    if (
        any(path.is_symlink() for path in (target, *target.parents))
        or not target.is_dir()
        or (target / INDEX).is_symlink()
    ):
        refuse("journal archive is not an owned directory")
    previous = strict_json.read(target / INDEX)
    if not isinstance(previous, dict) or {k: v for k, v in previous.items() if k != "state"} != {
        k: v for k, v in document.items() if k != "state"
    }:
        refuse("journal archive identity/output inventory differs")
    if previous.get("state") != document["state"] and not (
        previous.get("state") == "installed"
        and document["state"] == "committed"
        and document.get("intent") is None
        and document.get("git_commit") is None
        and not document.get("tail_outputs")
    ):
        refuse("journal archive state transition is not proved")
    for row in document["outputs"] + document.get("tail_outputs", []):
        if row["backup"] is not None:
            backup = target / row["backup"]
            if (
                backup.is_symlink()
                or not backup.is_file()
                or inputs.digest(backup, algorithm="sha256", reuse=False) != row["backup_sha256"]
            ):
                refuse("journal archive before-image unavailable")
    return target


def archive(directory: Path, document: dict[str, Any]) -> None:
    import json

    previous = _prior_archive(directory, document)
    target = directory.with_name(directory.name + ".archive")
    target.mkdir(parents=True, exist_ok=True)
    if previous is None:
        os.replace(directory, target / document["operation_id"])
    else:
        # Keep both interrupted indexes and every before-image in the same operation archive.
        snapshot = previous / uuid.uuid4().hex
        prior = (previous / INDEX).read_bytes()
        os.replace(directory, snapshot)
        atomic_files.write(snapshot / "prior-index.json", prior)
        atomic_files.write(previous / INDEX, json.dumps(document, sort_keys=True).encode())
    atomic_files.sync_directory(target)
    atomic_files.sync_directory(directory.parent)


def finalize(root: Path, document: dict[str, Any]) -> None:
    intent = document.get("intent")
    if intent is None or not intent["pending"]:
        return
    from unbake.config import load
    from unbake.work.attempts import Event, Outcome, dependency_record, ledger, now

    project = load(root)
    final_events = document.get("final_events")
    if final_events is None:
        final_events = {}
        for subject, event in intent["pending"].items():
            identity = uuid.uuid5(uuid.NAMESPACE_URL, document["operation_id"] + subject + ".committed").hex
            publication = dict(event["result"]["value"]["publication"])
            publication["committed_source_sha256"] = publication["stored_source_sha256"]
            final_events[subject] = Event(
                2,
                identity,
                identity,
                project.id,
                "publication." + publication["kind"],
                (event["event_id"],),
                subject,
                {"git_commit": document["git_commit"], "recovered_operation": document["operation_id"]},
                event["dependencies"],
                Outcome(identity, "committed", {"publication": publication}, None, {}).document(),
                {},
                now(),
            ).document()
        document["final_events"] = final_events
        import json

        atomic_files.write(Path(document["directory"]) / INDEX, json.dumps(document, sort_keys=True).encode())
    target = root / "attempts.jsonl"
    with target.open("rb") as stream:
        length = target.stat().st_size
        if length:
            stream.seek(-1, os.SEEK_END)
        last = stream.read(1)
    if length and last != b"\n":
        raw = target.read_bytes()
        from unbake.work.attempts import portable_tree, stored_record

        complete, _, tail = raw.rpartition(b"\n")
        if not any(
            stored_record(project, portable_tree(project, row)).startswith(tail) for row in final_events.values()
        ):
            refuse("accepted tree preserved; torn ledger tail is not this operation's declared outcome")
        atomic_files.write(Path(document["directory"]) / "torn-record.bin", tail)
        atomic_files.write(target, complete + b"\n" if complete else b"")
    history = ledger(project)
    history._refresh()
    tool = Path(__file__).parent
    for subject, event in intent["pending"].items():
        row = final_events[subject]
        if row["event_id"] in history.events:
            continue
        for name, digest in intent["tool_files"].items():
            if inputs.digest(tool / name, algorithm="sha256", reuse=False) != digest:
                refuse("accepted tree preserved; proof owner recipe changed before outcome recovery")
        dependencies = dependency_record(event["dependencies"])
        for pin in dependencies.files:
            if (
                pin.path.root not in ("project", project.id)
                or inputs.file_pin(root.joinpath(*pin.path.parts), root=root, root_id=pin.path.root, reuse=False) != pin
            ):
                refuse("accepted tree preserved; prepared publication input changed before outcome recovery")
        for name, digest in dependencies.values.get("native", {}).items():
            path = intent["native_paths"].get(name)
            if path is None or inputs.digest(Path(path), algorithm="sha256", reuse=False) != digest:
                refuse("accepted tree preserved; native input changed before outcome recovery")
        history.append(Event(**{**row, "parents": tuple(row["parents"])}))


def restore_rows(directory: Path, rows: list[dict[str, Any]]) -> list[Path]:
    restored = []
    for row in reversed(rows):
        path = Path(row["path"])
        if row["backup"] is None:
            if path.exists() or path.is_symlink():
                atomic_files.remove(path)
        else:
            atomic_files.write(path, (directory / row["backup"]).read_bytes(), mode=row["mode"])
            os.utime(path, ns=(row["atime_ns"], row["mtime_ns"]))
        restored.append(path)
    return restored


def recover(directory: Path, *, root: Path) -> list[Path]:
    """Recover only declared outputs; committed before-images remain preserved."""
    live = current()
    if live is not None:
        if live.root != root.resolve():
            refuse("recovery cannot change the live owning root")
        if live.directory.resolve() == directory.resolve():
            return []
    index = directory / INDEX
    if not index.is_file():
        return []
    if directory.is_symlink() or index.is_symlink():
        refuse("journal recovery requires an owned directory/index")
    document = strict_json.read(index)
    if not isinstance(document, dict) or document.get("schema") != 2:
        refuse("journal schema2 required; preserve old journal and use offline migration recovery")
    if (
        document.get("directory") != str(directory)
        or document.get("root") != str(root.resolve())
        or document.get("state") not in {"prepared", "installed", "committed"}
    ):
        refuse("journal root/state differs; preserve evidence")
    intent = document.get("intent")
    if intent is not None:
        result = process.run_native(
            [
                "git",
                "log",
                "-1",
                "--format=%H",
                "--fixed-strings",
                "--grep=Unbake-Operation: " + document["operation_id"],
            ],
            root,
            "journal",
            temporary_root=directory,
        )
        commit = result.stdout.strip()
        if commit:
            listing = process.run_native(
                ["git", "ls-tree", "-r", "-z", commit, "--", *intent["paths"]],
                root,
                "journal",
                temporary_root=directory,
            ).stdout
            tree = {}
            for row in listing.split("\0"):
                if row:
                    metadata, name = row.split("\t", 1)
                    mode, kind, blob = metadata.split()
                    tree[name] = (mode, kind, blob)
            for name, expected in intent["after"].items():
                actual = tree.get(name)
                if (expected is None and actual is not None) or (
                    expected is not None and actual != (expected["mode"], "blob", expected["blob"])
                ):
                    refuse("operation trailer commit differs from prepared output bytes/modes/deletions")
            document["state"], document["git_commit"] = "committed", commit
        elif document["state"] == "committed":
            refuse("recorded commit has no matching operation trailer")
        index_path = process.run_native(
            ["git", "rev-parse", "--path-format=absolute", "--git-path", "index"],
            root,
            "journal",
            temporary_root=directory,
        ).stdout.strip()
        if document.get("git_index") != index_path:
            refuse("Git index path differs from the owning repository")
    if (
        type(document.get("schema")) is not int
        or not isinstance(document.get("operation_id"), str)
        or not re.fullmatch(r"[0-9a-f]{32}", document["operation_id"])
        or not isinstance(document.get("outputs"), list)
    ):
        refuse("invalid journal identity/output inventory")
    rows = document["outputs"] + document.get("tail_outputs", [])
    expected_row = {"path", "backup", "backup_sha256", "mode", "atime_ns", "mtime_ns"}
    if any(
        not isinstance(row, dict)
        or set(row) != expected_row
        or not isinstance(row["path"], str)
        or (
            row["backup"] is not None
            and (
                not isinstance(row["backup"], str)
                or not row["backup"].isdigit()
                or not isinstance(row["backup_sha256"], str)
                or not re.fullmatch(r"[0-9a-f]{64}", row["backup_sha256"])
                or type(row["mode"]) is not int
                or not 0 <= row["mode"] <= 0o7777
                or type(row["atime_ns"]) is not int
                or type(row["mtime_ns"]) is not int
            )
        )
        for row in rows
    ):
        refuse("invalid declared output before-image metadata")
    if len({row["path"] for row in rows}) != len(rows):
        refuse("duplicate declared output")
    previous = _prior_archive(directory, document)
    backups = directory
    if previous is not None:
        backups = previous
    for row in rows:
        path = Path(row["path"])
        if (not path.is_relative_to(root.resolve()) and str(path) != document.get("git_index")) or any(
            parent.is_symlink() for parent in path.parents
        ):
            refuse("journal output escapes owning root")
        if row["backup"] is not None:
            backup = directory / row["backup"]
            if backup.is_symlink():
                refuse("journal before-image unavailable")
            if not backup.exists() and previous is not None:
                backup = previous / row["backup"]
            if (
                backup.parent not in {directory, previous}
                or not backup.is_file()
                or backup.is_symlink()
                or inputs.digest(backup, algorithm="sha256", reuse=False) != row["backup_sha256"]
            ):
                refuse("journal before-image unavailable")
    restored = restore_rows(
        backups, document["outputs"] if document["state"] != "committed" else document.get("tail_outputs", [])
    )
    if document["state"] == "committed":
        # Persist acceptance before finalizing: a later refusal must never roll back real Git.
        import json

        atomic_files.write(index, json.dumps(document, sort_keys=True).encode())
        finalize(root, document)
    archive(directory, document)
    return restored


class Journal:
    def __init__(self, directory: Path, *, root: Path) -> None:
        self.directory = directory
        self.root = root.resolve()
        self.operation_id = uuid.uuid4().hex
        self.rows: list[dict[str, object]] = []
        self.saved: set[Path] = set()
        self.tail_rows: list[dict[str, Any]] = []
        self.tail_saved: set[Path] = set()
        self.state = "prepared"
        self.saving = False
        self.git_commit: str | None = None
        self.intent: dict[str, Any] | None = None
        self.git_index: str | None = None
        self.pending_ids: set[str] = set()
        self.parent: Journal | None = None
        self.aborted = False
        self.recording: Any = None
        self.token: Any = None

    def document(self) -> dict[str, object]:
        return {
            "schema": 2,
            "operation_id": self.operation_id,
            "root": str(self.root),
            "directory": str(self.directory),
            "state": self.state,
            "git_commit": self.git_commit,
            "git_index": self.git_index,
            "intent": self.intent,
            "outputs": self.rows,
            "tail_outputs": self.tail_rows,
        }

    def persist(self) -> None:
        import json

        atomic_files.write(self.directory / INDEX, json.dumps(self.document(), sort_keys=True).encode())

    def __enter__(self) -> Journal:
        self.parent = current()
        if self.parent is not None:
            if self.parent.root != self.root:
                refuse("nested journal cannot change root")
            return self.parent
        for directory in sorted(self.root.joinpath("build").glob("*.journal")):
            if directory.is_dir() and (directory / INDEX).is_file():
                recover(directory, root=self.root)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.persist()
        self.token = _current.set(self)
        self.recording = atomic_files.recording(self.record)
        self.recording.__enter__()
        return self

    def save(self, paths: Iterable[Path]) -> None:
        """Declare every output before replacement, preserving bytes, absence and mode."""
        if self.saving:
            return
        self.saving = True
        rows = self.tail_rows if self.state == "committed" else self.rows
        saved = self.tail_saved if self.state == "committed" else self.saved
        try:
            for spelling in sorted(set(paths)):
                path = Path(os.path.abspath(spelling))
                if path in saved:
                    continue
                if (
                    (not path.is_relative_to(self.root) and str(path) != self.git_index)
                    or path.is_symlink()
                    or any(parent.is_symlink() for parent in path.parents)
                ):
                    refuse(f"journal output must be regular and inside root: {path}")
                backup = None
                info = path.stat() if path.exists() else None
                if info is not None and not stat.S_ISREG(info.st_mode):
                    refuse(f"journal output is not a regular file: {path}")
                if info is not None:
                    backup = str(len(self.rows) + len(self.tail_rows))
                    atomic_files.copyfile(path, self.directory / backup)
                rows.append(
                    {
                        "path": str(path),
                        "backup": backup,
                        "backup_sha256": inputs.digest(self.directory / backup, algorithm="sha256", reuse=False)
                        if backup is not None
                        else None,
                        "mode": stat.S_IMODE(info.st_mode) if info else None,
                        "atime_ns": info.st_atime_ns if info else None,
                        "mtime_ns": info.st_mtime_ns if info else None,
                    }
                )
                saved.add(path)
            self.persist()
        finally:
            self.saving = False

    def record(self, path: Path) -> None:
        if self.saving:
            return
        path = Path(os.path.abspath(path))
        if not path.is_relative_to(self.root):
            return
        relative = path.relative_to(self.root)
        if ".git" in relative.parts or any(part.endswith((".journal", ".journal.archive")) for part in relative.parts):
            return
        if relative.parts[:2] in {("build", "cache"), ("build", "work")}:
            return
        if path in (self.tail_saved if self.state == "committed" else self.saved):
            return
        self.save((path,))
        if self.state == "prepared":
            self.state = "installed"
            self.saving = True
            try:
                self.persist()
            finally:
                self.saving = False

    def prepare_commit(self, project: Any, host: Any, index: Path, paths: list[Path], base: str) -> str:
        """Bind the real Git commit to this operation before invoking Git."""
        from unbake.project import headers
        from unbake.work.attempts import ledger

        self.git_index = str(index.absolute())
        self.save([index])
        history = ledger(project)
        history._refresh()
        names = {str(path.relative_to(self.root)) for path in paths}
        pending = {}
        for identity in history.order:
            event = history.events[identity]
            if identity in self.pending_ids and event["kind"] == "publication.prepared":
                publication = event["result"]["value"]["publication"]
                if "/".join(publication["source"]["parts"]) in names:
                    pending[event["subject"]] = event
        native_paths = {
            field: str(Path(getattr(host, field)).resolve())
            for field in ("cpp", "mips_as", "mips_ld", "mips_objcopy", "n64link")
        }
        native_paths.update({"cc:" + name: str(compiler.cc.resolve()) for name, compiler in project.compilers.items()})
        tool = Path(__file__).parent
        recipes = {
            *headers.RECIPE_MODULES,
            "work/compare.py",
            "runner.py",
            "process.py",
            "compilers/drivers.py",
            "compilers/candidates.py",
        }
        self.intent = {
            "base": base,
            "paths": sorted(names),
            "after": {name: image(self.root / name) for name in names},
            "pending": pending,
            "native_paths": native_paths,
            "tool_files": {name: inputs.digest(tool / name, algorithm="sha256", reuse=False) for name in recipes},
        }
        self.saving = True
        try:
            self.persist()
        finally:
            self.saving = False
        return "Unbake-Operation: " + self.operation_id

    def rollback(self) -> None:
        self.aborted = True

    def commit(self, *, git_commit: str | None = None) -> None:
        self.state, self.git_commit = "committed", git_commit
        self.persist()

    def __exit__(
        self, kind: type[BaseException] | None, error: BaseException | None, trace: TracebackType | None
    ) -> None:
        if self.parent is not None:
            return
        self.saving = True
        try:
            if kind is None and not self.aborted and self.state != "committed":
                self.commit(git_commit=self.git_commit)
            self.recording.__exit__(kind, error, trace)
            _current.reset(self.token)
            if self.state == "committed":
                document = strict_json.read(self.directory / INDEX)
                if kind is not None:
                    restore_rows(self.directory, document.get("tail_outputs", []))
                finalize(self.root, document)
                archive(self.directory, document)
            else:
                recover(self.directory, root=self.root)
        finally:
            self.saving = False


def recover_all(project: Any) -> list[Path]:
    restored = []
    for directory in sorted(project.build.glob("*.journal")):
        if directory.is_dir() and (directory / INDEX).is_file():
            restored.extend(recover(directory, root=project.root))
    return restored


def current() -> Journal | None:
    return _current.get()


def prepared(event_id: str) -> None:
    operation = current()
    if operation is not None:
        operation.pending_ids.add(event_id)


def accepted(*, git_commit: str | None = None) -> None:
    transaction = current()
    if transaction is not None:
        if git_commit is None and transaction.state == "committed":
            return
        transaction.saving = True
        try:
            transaction.commit(git_commit=git_commit)
            finalize(transaction.root, transaction.document())
        finally:
            transaction.saving = False


@contextmanager
def transaction(project: Any) -> Iterator[Journal]:
    existing = current()
    if existing is not None:
        if existing.root != project.root.resolve():
            refuse("nested transaction cannot change owning root")
        yield existing
        return
    with Journal(project.build / "publication.journal", root=project.root) as changes:
        yield changes


F = TypeVar("F", bound=Callable[..., Any])


def transactional(function: F) -> F:
    @wraps(function)
    def guarded(project: Any, *args: Any, **kwargs: Any) -> Any:
        with transaction(project):
            return function(project, *args, **kwargs)

    return cast(F, guarded)


def scratch(parent: Path, prefix: str) -> Path:
    """A new private directory PARENT/PREFIX<pid>-<n>; directories of processes that no longer exist are removed
    first, so a killed process leaks nothing past the next run."""
    parent.mkdir(parents=True, exist_ok=True)
    for stale in parent.glob(prefix + "*"):
        owner = stale.name[len(prefix) :].split("-", 1)[0]
        if owner.isdigit() and not _alive(int(owner)):
            shutil.rmtree(stale, ignore_errors=True)
    for number in range(1 << 16):
        path = parent / f"{prefix}{os.getpid()}-{number}"
        try:
            path.mkdir()
        except FileExistsError:
            continue
        return path
    raise FileExistsError(f"{parent}/{prefix}{os.getpid()}-*: no free name")


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
