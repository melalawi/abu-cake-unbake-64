"""Run one external tool and name its failure; read text inputs and name their failure."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import select
import signal
import subprocess
import sys
import traceback
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from unbake.inputs import DependencySet

from unbake.config import Held


@dataclass(frozen=True)
class SourceLocation:
    path: str
    line: int | None = None
    column: int | None = None


@dataclass(frozen=True)
class Action:
    kind: Literal["command", "edit", "stop"]
    argv: tuple[str, ...] = ()
    paths: tuple[str, ...] = ()
    reason: str = ""

    def render(self, context: Any) -> str:
        if self.kind == "command":
            words = tuple(
                str(context.root / word.removeprefix("project:")) if word.startswith("project:") else word
                for word in self.argv
            )
            return str(context.cmd(*words))
        reason = self.reason.removeprefix("stop: ")
        return "stop: " + reason


@dataclass(frozen=True)
class RetryRule:
    kind: Literal["dependencies", "worker-generation", "never"]
    watch: tuple[str, ...] = ()


@dataclass(frozen=True)
class Cause:
    key: str
    owner: str
    stage: str
    subject: str
    reason: str
    location: SourceLocation | None
    dependency_set: DependencySet
    retry: RetryRule
    action: Action
    evidence: Mapping[str, Any] = field(default_factory=dict)

    @property
    def id(self) -> str:
        stable = {
            "owner": self.owner,
            "key": self.key,
            "stage": self.stage,
            "subject": self.subject,
            "location": asdict(self.location) if self.location else None,
            "signature": self.evidence.get("signature"),
        }
        return hashlib.sha256(
            json.dumps(stable, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
        ).hexdigest()

    @property
    def blocked_key(self) -> str:
        from unbake.cache import key

        return key(self.id, self.dependency_set.digest)

    @property
    def retryability(self) -> str:
        return "unknown" if self.dependency_set.values.get("dependencies_unknown") else self.retry.kind

    def document(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "owner": self.owner,
            "stage": self.stage,
            "subject": self.subject,
            "reason": self.reason,
            "location": asdict(self.location) if self.location else None,
            "dependency_set": self.dependency_set.document(),
            "retry": asdict(self.retry),
            "action": asdict(self.action),
            "evidence": dict(self.evidence),
            "cause_id": self.id,
            "blocked_key": self.blocked_key,
            "retryability": self.retryability,
        }

    @classmethod
    def read(cls, value: Mapping[str, Any]) -> Cause:
        from unbake.inputs import DependencySet, FilePin, LogicalPath

        required = {
            "key",
            "owner",
            "stage",
            "subject",
            "reason",
            "location",
            "dependency_set",
            "retry",
            "action",
            "evidence",
        }
        if required - value.keys():
            raise ValueError("fault.cause: incomplete explicit cause")
        for name in ("key", "owner", "stage", "subject", "reason"):
            if not isinstance(value[name], str) or not value[name]:
                raise ValueError("fault.cause: explicit nonempty " + name + " required")
        if (
            value["retry"]["kind"] not in ("dependencies", "worker-generation", "never")
            or not isinstance(value["retry"]["watch"], (tuple, list))
            or any(not isinstance(k, str) for k in value["retry"]["watch"])
        ):
            raise ValueError("fault.retry: explicit rule required")
        if value["action"]["kind"] not in ("command", "edit", "stop") or not isinstance(value["evidence"], Mapping):
            raise ValueError("fault.action: typed action and evidence required")
        dependencies = value["dependency_set"]
        pins = tuple(
            FilePin(
                LogicalPath(row["path"]["root"], tuple(row["path"]["parts"])),
                row["state"],
                row["sha256"],
                LogicalPath(row["link_target"]["root"], tuple(row["link_target"]["parts"]))
                if row["link_target"]
                else None,
            )
            for row in dependencies["files"]
        )
        return cls(
            value["key"],
            value["owner"],
            value["stage"],
            value["subject"],
            value["reason"],
            SourceLocation(**value["location"]) if value["location"] else None,
            DependencySet(pins, dependencies["values"], dependencies["recipes"]),
            RetryRule(value["retry"]["kind"], tuple(value["retry"]["watch"])),
            Action(
                value["action"]["kind"],
                tuple(value["action"]["argv"]),
                tuple(value["action"]["paths"]),
                value["action"]["reason"],
            ),
            value["evidence"],
        )


@dataclass(frozen=True)
class Frame:
    kind: Literal["context", "python", "native"]
    owner: str
    stage: str
    reason: str
    evidence: Mapping[str, Any] = field(default_factory=dict)

    def document(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "owner": self.owner,
            "stage": self.stage,
            "reason": self.reason,
            "evidence": dict(self.evidence),
        }


@dataclass(frozen=True)
class Fault:
    cause: Cause
    chain: tuple[Frame | NativeResult | Cause, ...] = ()

    def document(self) -> dict[str, Any]:
        frames = []
        for item in self.chain:
            if isinstance(item, NativeResult):
                frames.append({"kind": "native", "result": asdict(item)})
            elif isinstance(item, Cause):
                frames.append({"kind": "cause", "cause": item.document()})
            else:
                frames.append(item.document())
        return {"schema": 2, "cause": self.cause.document(), "chain": frames}

    def framed(self, owner: str, stage: str, reason: str, evidence: Mapping[str, Any] | None = None) -> Fault:
        return replace(self, chain=(*self.chain, Frame("context", owner, stage, reason, evidence or {})))

    @classmethod
    def read(cls, value: Mapping[str, Any]) -> Fault:
        if (
            set(value) != {"schema", "cause", "chain"}
            or type(value.get("schema")) is not int
            or value.get("schema") != 2
        ):
            raise ValueError("fault.schema: schema=2 required; use offline state migration")
        frames: list[Frame | NativeResult | Cause] = []
        for row in value["chain"]:
            if row["kind"] == "native":
                data = dict(row["result"])
                data["args"] = tuple(data["args"])
                frames.append(NativeResult(**data))
            elif row["kind"] == "cause":
                frames.append(Cause.read(row["cause"]))
            else:
                frames.append(Frame(row["kind"], row["owner"], row["stage"], row["reason"], row["evidence"]))
        return cls(Cause.read(value["cause"]), tuple(frames))


@dataclass(frozen=True)
class CauseScope:
    subject: str
    dependencies: DependencySet
    complete: bool


_scope: ContextVar[CauseScope | None] = ContextVar("unbake_cause_scope", default=None)


@contextmanager
def cause_scope(subject: str, dependencies: DependencySet, *, complete: bool = True) -> Any:
    token = _scope.set(CauseScope(subject, dependencies, complete))
    try:
        yield
    finally:
        _scope.reset(token)


def named(
    key: str,
    reason: str,
    *,
    owner: str,
    stage: str,
    subject: str | None = None,
    dependencies: DependencySet | None = None,
    action: Action | None = None,
    retry: RetryRule | None = None,
    evidence: Mapping[str, Any] | None = None,
    location: SourceLocation | None = None,
) -> Cause:
    """Owner supplies the explicit key; request scope supplies only proven dependency context."""
    from unbake.inputs import DependencySet

    scope = _scope.get()
    deps = dependencies or (
        scope.dependencies if scope and scope.complete else DependencySet((), {"dependencies_unknown": True}, {})
    )
    return Cause(
        key,
        owner,
        stage,
        subject or (scope.subject if scope else owner),
        reason,
        location,
        deps,
        retry
        or RetryRule(
            "dependencies",
            (
                *tuple(p.path.name for p in deps.files),
                *tuple("value:" + k for k in deps.values),
                *tuple("recipe:" + k for k in deps.recipes),
            ),
        ),
        action or Action("stop", reason=reason),
        evidence or {},
    )


def capture(error: BaseException, *, cause: Cause) -> Fault:
    """Preserve the first owning cause and add typed context, never search nested dicts for a cause."""
    if isinstance(error, Held):
        return error.fault if error.fault.cause == cause else error.fault.framed(cause.owner, cause.stage, cause.reason)
    frames = tuple(
        {"file": Path(f.filename).name, "line": f.lineno, "function": f.name}
        for f in traceback.extract_tb(error.__traceback__)
    )
    evidence = {"type": type(error).__name__, "frames": frames, "reason": str(error)}
    if isinstance(error.__cause__, Held):
        return error.__cause__.fault.framed(cause.owner, cause.stage, cause.reason, evidence)
    return Fault(cause, (Frame("python", cause.owner, cause.stage, str(error), evidence),))


def attached(cause: Cause, fault: Fault | Mapping[str, Any] | None) -> Fault:
    if fault is None:
        return Fault(cause)
    current = Fault.read(fault) if isinstance(fault, Mapping) else fault
    return current.framed(cause.owner, cause.stage, cause.reason)


def native_results(fault: Fault) -> tuple[NativeResult, ...]:
    return tuple(frame for frame in fault.chain if isinstance(frame, NativeResult))


def temporary_environment(work: Path, env: Mapping[str, str] | None = None) -> dict[str, str]:
    """Pin child scratch, including SQLite spill files, to the caller's explicit storage."""
    directory = work.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    return dict(
        os.environ if env is None else env,
        TMPDIR=str(directory),
        TMP=str(directory),
        TEMP=str(directory),
        SQLITE_TMPDIR=str(directory),
    )


@dataclass(frozen=True)
class NativeResult:
    args: tuple[str, ...]
    cwd: str
    exit: int | None
    signal: int | None
    stdout: str
    stderr: str
    category: str
    errno: int | None
    encoding: str
    errors: str
    context: dict[str, Any]


def run_native(
    argv: list[str],
    work: Path,
    phase: str,
    *,
    context: dict[str, Any] | None = None,
    env: dict[str, str] | None = None,
    temporary_root: Path | None = None,
    stdin: str | None = None,
) -> NativeResult:
    """The one native result/fault boundary, retaining both streams and exact invocation."""
    from unbake import effort

    effort.count("native.calls", 1, 1)
    environment = dict(temporary_environment(work if temporary_root is None else temporary_root, env), LC_ALL="C")
    key = f"{phase}.{Path(argv[0]).name}"
    native_input: dict[str, Any] = {"input": stdin} if stdin is not None else {}
    try:
        completed = subprocess.run(
            argv,
            cwd=work,
            env=environment,
            **native_input,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="surrogateescape",
        )
    except OSError as error:
        result = NativeResult(
            tuple(argv),
            str(work),
            None,
            None,
            "",
            str(error),
            "native-os",
            error.errno,
            "utf-8",
            "surrogateescape",
            context or {},
        )
        raise Held(
            Fault(
                named(f"{key}", f"{key}: {Path(argv[0]).name}: {error.strerror}", owner="process", stage=phase),
                (result,),
            )
        ) from error
    status = completed.returncode
    result = NativeResult(
        tuple(argv),
        str(work),
        status if status >= 0 else None,
        -status if status < 0 else None,
        completed.stdout,
        completed.stderr,
        "success" if status == 0 else "native-signal" if status < 0 else "native-exit",
        None,
        "utf-8",
        "surrogateescape",
        context or {},
    )
    if status:
        detail = next(iter((completed.stderr or completed.stdout).strip().splitlines()), "no diagnostic")
        cause = f"signal {-status}" if status < 0 else f"exit {status}"
        raise Held(
            Fault(
                named(
                    f"{key}", f"{key}: {Path(argv[0]).name} failed ({cause}): {detail}", owner="process", stage=phase
                ),
                (result,),
            )
        )
    return result


def git_pathspec(argv: list[str]) -> tuple[list[str], str | None]:
    """Keep large add/commit path sets off argv, preserving one atomic Git operation."""
    if "--" not in argv or not ({"add", "commit"} & set(argv[:argv.index("--")])):
        return argv, None
    offset = argv.index("--")
    paths = argv[offset + 1:]
    # Leave ample room for the environment and pointer table on small ARG_MAX hosts.
    if sum(len(os.fsencode(value)) + 9 for value in argv) <= 32768:
        return argv, None
    return [*argv[:offset], "--pathspec-from-file=-", "--pathspec-file-nul"], "\0".join(paths) + "\0"


def path_batches(paths: Sequence[str]) -> Iterator[list[str]]:
    """Bound path argv for read-only Git commands without pathspec-file support."""
    batch: list[str] = []
    size = 0
    for path in paths:
        cost = len(os.fsencode(path)) + 9
        if batch and size + cost > 32768:
            yield batch
            batch, size = [], 0
        batch.append(path)
        size += cost
    if batch:
        yield batch


def run_tool(
    argv: list[str],
    work: Path,
    phase: str,
    *,
    context: dict[str, Any] | None = None,
    temporary_root: Path | None = None,
    stdin: str | None = None,
) -> str:
    return run_native(argv, work, phase, context=context, temporary_root=temporary_root, stdin=stdin).stdout


def read_text(path: Path, phase: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise Held(named(f"{path}", f"{path}: {error}", owner="process", stage=phase)) from error


def owner(server: int) -> int:
    """The process that started the fork server `server`: the one that owns the pool."""
    stat = Path(f"/proc/{server}/stat").read_text()
    return int(stat.rsplit(")", 1)[1].split()[1])


def orphaned(descriptor: int | None, server: int) -> None:
    """When the owner exits, kill the fork server and this worker's group. A stopped owner often takes the server
    down first: its absence must never spare the group (orphan workers kept running and holding memory)."""
    if descriptor is not None:
        select.select([descriptor], [], [])
    with contextlib.suppress(ProcessLookupError):
        os.kill(server, signal.SIGKILL)
    with contextlib.suppress(ProcessLookupError):
        os.killpg(0, signal.SIGKILL)


def kill_groups(pids: Sequence[int]) -> None:
    """SIGKILL each worker's process group (the worker itself when its group does not exist yet)."""
    for pid in pids:
        try:
            os.killpg(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)


# Runs outside the permuter's group: when the owner (argv 1) exits, even by SIGKILL, the group (argv 2) dies.
_WATCH = (
    "import os, select, signal, sys\n"
    "select.select([os.pidfd_open(int(sys.argv[1]))], [], [])\n"
    "try:\n"
    "    os.killpg(int(sys.argv[2]), signal.SIGKILL)\n"
    "except ProcessLookupError:\n"
    "    pass\n"
)


@contextmanager
def managed_group(
    argv: Sequence[str], *, cwd: Path, env: Mapping[str, str], stdout: Any, stderr: Any
) -> Iterator[subprocess.Popen[Any]]:
    """One owner-death watcher and teardown for a native process tree."""
    child = subprocess.Popen(argv, cwd=cwd, env=dict(env), stdout=stdout, stderr=stderr, start_new_session=True)
    watcher = None
    try:
        watcher = subprocess.Popen(
            [sys.executable, "-c", _WATCH, str(os.getpid()), str(child.pid)], env=dict(env), start_new_session=True
        )
        yield child
    finally:
        kill_groups([child.pid])
        child.wait()
        if watcher is not None:
            watcher.kill()
            watcher.wait()
