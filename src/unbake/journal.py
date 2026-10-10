import base64
import fcntl
import json
import os
import tempfile
from pathlib import Path

from unbake import effort, process
from unbake.contracts import Config, Finding, Plan, Refusal


def _install(path: Path, content: bytes | None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if content is None:
        path.unlink(missing_ok=True)
    elif _read(path) == content:
        return
    else:
        fd, name = tempfile.mkstemp(prefix=".unbake-", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, path)
        finally:
            Path(name).unlink(missing_ok=True)
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)  # the directory entry is durable too
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _read(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def _git(config: Config, *args: str) -> bytes:
    result = process.git(config, *args)
    if result.exit != 0:
        raise Refusal(Finding("journal.changed", "Git could not complete the journal operation.",
                              missing=(os.fsdecode(result.stderr),)))
    return result.stdout


def _status(config: Config, paths: list[str]) -> tuple[str, ...]:
    if not paths:
        return ()
    entries = iter(_git(config, "--literal-pathspecs", "status", "--porcelain", "-z", "--", *paths).split(b"\0"))
    changed = []
    for entry in entries:
        if entry:
            changed.append(os.fsdecode(entry[3:]))
            if b"R" in entry[:2] or b"C" in entry[:2]:
                changed.append(os.fsdecode(next(entries)))
    return tuple(changed)


def apply(config: Config, plan: Plan, head: str) -> str:
    with effort.stage("journal.apply"):
        if plan.blocking:
            raise ValueError("A blocking plan cannot be applied.")
        root = config.project.root
        journal = root / ".unbake" / "journal.json"
        journal.parent.mkdir(parents=True, exist_ok=True)
        with (journal.parent / "journal.lock").open("a+b") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            _rollback(config)  # a drain no longer recovers first: whoever commits clears what a crash left behind
            paths = sorted(plan.writes)
            now = _git(config, "rev-parse", "HEAD").decode().strip()
            changed = _status(config, paths)
            if now != head or changed:
                missing = changed + ((f"HEAD moved from {head} to {now}",) if now != head else ())
                raise Refusal(Finding("journal.changed", "The project changed under a planned write.", missing=missing))
            # a file the plan changes is clean (checked above), so git has its old bytes: only its name is recorded
            tracked = set(_git(config, "--literal-pathspecs", "ls-files", "-z", "--", *paths).decode().split("\0"))
            manifest = {"head": head, "plan": plan.digest, "before": {
                path: "git" if path in tracked else (base64.b64encode(old).decode() if (old := _read(root / path))
                                                     is not None else None) for path in paths}}
            _install(journal, json.dumps(manifest, sort_keys=True).encode())
            for path in paths:
                _install(root / path, plan.writes[path])
            if paths:
                _git(config, "--literal-pathspecs", "add", "-A", "--", *paths)
            _git(config, "-c", f"user.name={config.host.author[0]}", "-c", f"user.email={config.host.author[1]}",
                 "commit", "-m", plan.message, "-m", f"plan {plan.digest}")
            new = _git(config, "rev-parse", "HEAD").decode().strip()
            mismatched = tuple(path for path in paths if _read(root / path) != plan.writes[path])
            dirty = _status(config, paths)
            if mismatched or dirty:
                raise Refusal(Finding("journal.readback", "Committed files differ from the plan.",
                                      missing=tuple(dict.fromkeys(mismatched + dirty))))
            _install(journal, None)
            return new


def _rollback(config: Config) -> bool:
    """Under journal.lock: put back what a crashed apply left half written, or accept the commit it did make."""
    root = config.project.root
    journal = root / ".unbake" / "journal.json"
    content = _read(journal)
    if content is None:
        return False
    manifest = json.loads(content)
    rollback = _git(config, "rev-parse", "HEAD").decode().strip() == manifest["head"]
    if rollback:
        for path, old in sorted(manifest["before"].items()):
            _install(root / path, _git(config, "show", f"{manifest['head']}:{path}") if old == "git" else
                     base64.b64decode(old) if old is not None else None)
    elif f"plan {manifest['plan']}" not in _git(config, "log", "-1", "--format=%B").decode().splitlines():
        raise Refusal(Finding("journal.changed", "HEAD moved without the journal's planned commit."))
    _install(journal, None)
    return rollback
def recover(config: Config) -> bool:
    with effort.stage("journal.recover"):
        lock = config.project.root / ".unbake" / "journal.lock"
        lock.parent.mkdir(parents=True, exist_ok=True)
        with lock.open("a+b") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            return _rollback(config)
