"""Content-addressed drafts and their append-only trial history."""

from __future__ import annotations

import fcntl
import hashlib
import json
import re
import tempfile
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypedDict, cast

from unbake.decomp.candidate_ranking import candidate_rank
from unbake.decomp.score import percent, weakest
from unbake.layout import split
from unbake.project.config import Held, Policy, Project

if TYPE_CHECKING:
    from unbake.decomp.trial import Trial


class ComparisonRecord(TypedDict):
    version: str
    identical: int
    of: int
    typed: dict[str, int]
    lines: list[str]


class TrialRecord(TypedDict):
    function: str
    source_sha256: str
    sha256: str
    compares: dict[str, ComparisonRecord]
    score: dict[str, float]
    preconditions: list[str]
    next_command: str
    identical_everywhere: bool
    at: str
    work: dict[str, Any]


def rank(row: TrialRecord) -> tuple[bool, int, int, float]:
    return candidate_rank(
        row["identical_everywhere"],
        sum(item["identical"] for item in row["compares"].values()),
        sum(sum(item["typed"].values()) for item in row["compares"].values()),
        row["score"],
    )


def _function(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
        raise Held("drafts", f"function {value!r} must be a C identifier")
    return value


def _required(obj: object, name: str) -> Any:
    value = getattr(obj, name, None)
    if value is None:
        raise Held("drafts", f"trial.{name} is missing")
    return value


def _count(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise Held("drafts", f"{name} must be a nonnegative integer")
    return value


class Store:
    def __init__(self, policy: Policy, project: Project) -> None:
        state_root = getattr(policy, "state_root", None)
        if not state_root:
            raise Held("drafts", "policy.state_root is missing")
        name = getattr(project, "name", None)
        if not isinstance(name, str) or not name or Path(name).name != name or name in (".", ".."):
            raise Held("drafts", "project.name must be a single directory name")
        for key in ("id", "workspace_id"):
            value = getattr(project, key, None)
            if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f-]{36}", value):
                raise Held("drafts", f"project.{key}: required explicit UUID")
        self.policy = policy
        self.project = project
        self.root = Path(state_root) / project.id / project.workspace_id / "drafts"

    def add(self, trial: Trial, source: Path, score: dict[str, float]) -> str:
        function = _function(_required(trial, "function"))
        try:
            content = Path(source).read_bytes()
        except (OSError, TypeError) as error:
            raise Held("drafts", f"source {source}: {error}") from error
        sha = hashlib.sha256(content).hexdigest()
        identity = source_identity(content)
        if _required(trial, "source_sha256") != identity:
            raise Held("drafts", f"trial.source_sha256 does not match source {source}")
        compares = _required(trial, "compares")
        if not isinstance(compares, dict) or not compares:
            raise Held("drafts", "trial.compares must contain at least one VERSION")
        if not isinstance(score, dict) or set(score) != set(compares):
            raise Held("drafts", "score VERSIONs must equal trial.compares VERSIONs")
        comparisons: dict[str, ComparisonRecord] = {}
        scores = {}
        for version, comparison in compares.items():
            self.project.version(version)
            if _required(comparison, "version") != version:
                raise Held("drafts", f"trial.compares[{version}].version differs")
            identical = _count(_required(comparison, "identical"), f"compares[{version}].identical")
            total = _count(_required(comparison, "of"), f"compares[{version}].of")
            if total == 0 or identical > total:
                raise Held("drafts", f"compares[{version}].of must be positive and at least identical")
            typed = _required(comparison, "typed")
            if not isinstance(typed, dict):
                raise Held("drafts", f"compares[{version}].typed must be a dictionary")
            for kind in ("register", "order", "immediate", "relocation", "inserted", "missing", "changed"):
                if kind not in typed:
                    raise Held("drafts", f"compares[{version}].typed.{kind} is missing")
                _count(typed[kind], f"compares[{version}].typed.{kind}")
            lines = _required(comparison, "lines")
            if not isinstance(lines, list) or any(not isinstance(line, str) for line in lines):
                raise Held("drafts", f"compares[{version}].lines must be a list of strings")
            comparisons[version] = {
                "version": version,
                "identical": identical,
                "of": total,
                "typed": typed,
                "lines": lines,
            }
            scores[version] = percent(score[version], f"score[{version}]", "drafts")
        preconditions = _required(trial, "preconditions")
        if not isinstance(preconditions, list) or any(not isinstance(item, str) for item in preconditions):
            raise Held("drafts", "trial.preconditions must be a list of strings")
        next_command = _required(trial, "next_command")
        if not isinstance(next_command, str) or not next_command:
            raise Held("drafts", "trial.next_command is missing")
        identical_everywhere = _required(trial, "identical_everywhere")
        proven = all(
            scores[version] == 100 and item["identical"] == item["of"] and not any(item["typed"].values())
            for version, item in comparisons.items()
        )
        if not isinstance(identical_everywhere, bool) or identical_everywhere != proven:
            raise Held("drafts", "trial.identical_everywhere differs from compares word counts/differences")
        row: TrialRecord = {
            "function": function,
            "source_sha256": identity,
            "sha256": sha,
            "compares": comparisons,
            "score": scores,
            "preconditions": preconditions,
            "next_command": next_command,
            "identical_everywhere": identical_everywhere,
            "at": datetime.now(UTC).isoformat(),
            "work": dict(_required(trial, "work_identity")),
        }
        if not row["work"]:
            from unbake.decomp.work import identity as work_identity

            row["work"] = dict(work_identity(self.project, Path(source).resolve(), list(compares), policy=self.policy))
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            with (self.root / "trials.jsonl").open("a+", encoding="utf-8") as ledger:
                fcntl.flock(ledger, fcntl.LOCK_EX)
                path = self.root / sha / f"{function}.c"
                path.parent.mkdir(exist_ok=True)
                if path.exists():
                    if path.read_bytes() != content:
                        raise Held("drafts", f"draft sha256 {sha} is corrupt at {path}")
                else:
                    _write(path, content)
                ledger.write(json.dumps(row, sort_keys=True, allow_nan=False) + "\n")
                ledger.flush()
        except OSError as error:
            raise Held("drafts", f"draft store {self.root}: {error}") from error
        return sha

    def rows(self, function: str) -> list[TrialRecord]:
        function = _function(function)
        return [row for row in self.history() if row["function"] == function]

    def history(self) -> list[TrialRecord]:
        """Read every validated trial record in append order."""
        path = self.root / "trials.jsonl"
        try:
            stream = path.open(encoding="utf-8")
        except FileNotFoundError:
            return []
        except OSError as error:
            raise Held("drafts", f"trial ledger {path}: {error}") from error
        rows: list[TrialRecord] = []
        try:
            with stream:
                fcntl.flock(stream, fcntl.LOCK_SH)
                for number, line in enumerate(stream, 1):
                    try:
                        row = json.loads(line)
                    except ValueError as error:
                        raise Held("drafts", f"{path}:{number}: invalid JSON") from error
                    if not isinstance(row, dict):
                        raise Held("drafts", f"{path}:{number}: trial row must be an object")
                    for name in (
                        "function",
                        "source_sha256",
                        "sha256",
                        "compares",
                        "score",
                        "preconditions",
                        "next_command",
                        "identical_everywhere",
                        "at",
                        "work",
                    ):
                        if name not in row:
                            raise Held("drafts", f"{path}:{number}: {name} is missing")
                    if (row["work"].get("schema"), row["work"].get("project_id"), row["work"].get("workspace_id")) != (
                        1,
                        self.project.id,
                        self.project.workspace_id,
                    ):
                        raise Held("drafts", f"{path}:{number}: trial.identity: incompatible work record")
                    _function(row["function"])
                    sha = row["sha256"]
                    if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{64}", sha):
                        raise Held("drafts", f"{path}:{number}: sha256 is invalid")
                    if row["source_sha256"] != sha:
                        content = (self.root / sha / f"{row['function']}.c").read_bytes()
                        if (
                            hashlib.sha256(content).hexdigest() != sha
                            or source_identity(content) != row["source_sha256"]
                        ):
                            raise Held("drafts", f"{path}:{number}: source_sha256 differs from canonical source")
                    weakest(row["score"])
                    if not isinstance(row["compares"], dict) or set(row["compares"]) != set(row["score"]):
                        raise Held("drafts", f"{path}:{number}: compares VERSIONs differ from score")
                    for version, comparison in row["compares"].items():
                        if not isinstance(comparison, dict) or "identical" not in comparison:
                            raise Held("drafts", f"{path}:{number}: compares[{version}].identical is missing")
                        _count(comparison["identical"], f"{path}:{number}: compares[{version}].identical")
                    rows.append(cast(TrialRecord, row))
        except (OSError, UnicodeError) as error:
            raise Held("drafts", f"trial ledger {path}: {error}") from error
        return rows

    def best(self, function: str) -> Path | None:
        rows = self.rows(function)
        if not rows:
            return None
        latest = {row["sha256"]: row for row in rows}
        sha = min(latest.values(), key=lambda row: (rank(row), row["sha256"]))["sha256"]
        path = self.root / sha / f"{function}.c"
        try:
            if hashlib.sha256(path.read_bytes()).hexdigest() != sha:
                raise Held("drafts", f"draft sha256 {sha} is corrupt at {path}")
        except OSError as error:
            raise Held("drafts", f"draft sha256 {sha} at {path}: {error}") from error
        return path

    def publish_all(self) -> list[Path]:
        """Publish best drafts in source files selected only by NON_MATCHING builds."""
        destination = getattr(self.project, "src", None)
        if not destination:
            raise Held("drafts", "project.src is missing")
        paths = []
        try:
            for function in sorted({row["function"] for row in self.history()}):
                path = Path(destination) / f"{function}.c"
                if path.exists() and not is_partial(path.read_text()):
                    continue
                source = self.best(function)
                if source is None:
                    raise Held("drafts", f"best.{function} is missing")
                content = source.read_text()
                if is_partial(content):
                    content = unguard(content)
                path.parent.mkdir(parents=True, exist_ok=True)
                _write(path, ("#ifdef NON_MATCHING\n" + content.rstrip("\n") + "\n#endif\n").encode())
                paths.append(path)
            return paths
        except (OSError, UnicodeError) as error:
            raise Held("drafts", f"publish drafts {destination}: {error}") from error


def canonical_source(content: bytes) -> bytes:
    """Remove only the complete publication wrapper; preserve all source bytes."""
    text = content.decode("utf-8")
    return unguard(text).encode("utf-8") if is_partial(text) else content


def source_identity(content: bytes) -> str:
    """Share exact source identity between trials and match submission."""
    return hashlib.sha256(canonical_source(content)).hexdigest()


def _partial_bounds(source: str) -> tuple[int, int] | None:
    """Recognize a complete wrapper after comments/includes, preserving offsets."""
    clean = re.sub(r"/\*.*?\*/|//[^\n]*", lambda match: "\n" * match[0].count("\n"), source, flags=re.S)
    lines = clean.splitlines()
    start = next((index for index, line in enumerate(lines) if line.strip() == "#ifdef NON_MATCHING"), None)
    if start is None or any(line.strip() and not re.fullmatch(r"\s*#\s*include\b.*", line) for line in lines[:start]):
        return None
    depth = 1
    for index, line in enumerate(lines[start + 1 :], start + 1):
        directive = re.match(r"\s*#\s*(if|ifdef|ifndef|endif|else|elif)\b", line)
        if directive:
            kind = directive[1]
            if depth == 1 and kind in ("else", "elif"):
                return None
            depth += 1 if kind in ("if", "ifdef", "ifndef") else -1 if kind == "endif" else 0
            if depth == 0:
                return (
                    (start, index)
                    if line.strip() == "#endif" and not any(tail.strip() for tail in lines[index + 1 :])
                    else None
                )
    return None


def is_partial(source: str) -> bool:
    """Recognize the complete publication wrapper, preserving nested directives."""
    return _partial_bounds(source) is not None


def unguard(source: str) -> str:
    """Remove exactly the outer partial wrapper or refuse its malformed form."""
    bounds = _partial_bounds(source)
    if bounds is None:
        raise Held("drafts", "NON_MATCHING.guard is missing or invalid")
    start, end = bounds
    return "".join(line for index, line in enumerate(source.splitlines(keepends=True)) if index not in (start, end))


def match_edits(project: Project, function: str, source_text: str, versions: Iterable[str]) -> list[split.Edit]:
    """Resolve publication removal and assembly rows into reviewable source/split edits."""
    function = _function(function)
    if not isinstance(source_text, str) or not source_text.strip():
        raise Held("drafts", "source_text is missing")
    if not versions:
        raise Held("drafts", "versions is missing")
    destination = getattr(project, "src", None)
    if not destination:
        raise Held("drafts", "project.src is missing")
    versions = tuple(versions)
    path = Path(destination) / f"{function}.c"
    try:
        before = path.read_text() if path.exists() else ""
        if before and not is_partial(before):
            raise Held("drafts", f"{function}: matched source already exists")
        content = unguard(source_text) if is_partial(source_text) else source_text
        edits = [split.Edit(path, before, content, versions)]
        pattern = re.compile(
            r"^(\s*-\s*\[\s*(?:0[xX][\da-fA-F]+|\d+)\s*,\s*)(asm|c)(\s*,\s*)([^,\]\n]+)([^\n]*\]\s*)$", re.M
        )
        for version in versions:
            split_path = project.version(version).split
            text = split_path.read_text()
            count = 0

            def replace(match: re.Match[str], version: str = version) -> str:
                nonlocal count
                name = match[4].strip().strip("\"'")
                if Path(name).name != function:
                    return match[0]
                count += 1
                if match[2] != "asm":
                    raise Held("drafts", f"{function}: VERSION {version} row requires asm")
                return match[1] + "c" + match[3] + function + match[5]

            after = pattern.sub(replace, text)
            if count != 1:
                raise Held("drafts", f"{function}: VERSION {version} requires one asm row, found {count}")
            edits.append(split.Edit(split_path, text, after, (version,)))
        return edits
    except (OSError, UnicodeError) as error:
        raise Held("drafts", f"{function}: {error}") from error


def _write(path: Path, content: bytes) -> None:
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(content)
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
