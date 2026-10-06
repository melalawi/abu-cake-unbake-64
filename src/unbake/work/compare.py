"""compare: compile one file for every holding version, link it alone at its address and compare with the ROM."""

from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from unbake import process
from unbake.config import Held, Host, Project, draft_view
from unbake.layout import split
from unbake.work import attempts
from unbake.work.score import TYPES, Compare, compare_words


@dataclass
class Compared:
    function: str
    file: Path
    source_sha256: str
    compares: dict[str, Compare]
    preconditions: list[str] = field(default_factory=list)
    seconds: float = 0.0
    compiler: str = ""
    # The preconditions in plain words (the raw messages stay in preconditions for the JSON document).
    rule_lines: list[str] = field(default_factory=list)
    faults: dict[str, dict[str, Any]] = field(default_factory=dict)
    required_versions: tuple[str, ...] | None = None

    @property
    def required_exact(self) -> bool:
        return (
            self.exact
            if self.required_versions is None
            else (
                bool(self.required_versions)
                and not self.preconditions
                and all(v in self.compares and self.compares[v].exact for v in self.required_versions)
            )
        )

    @property
    def identical_everywhere(self) -> bool:
        return bool(self.compares) and all(result.exact for result in self.compares.values())

    @property
    def exact(self) -> bool:
        return self.identical_everywhere and not self.preconditions

    @property
    def best_percent(self) -> float:
        return min((result.match_percent for result in self.compares.values()), default=0.0)

    @property
    def next_command(self) -> str:
        import shlex

        scope = "".join(" --require-version " + shlex.quote(v) for v in self.required_versions or ())
        return f"unbake {'publish' if self.required_exact else 'compare'} {shlex.quote(str(self.file))}{scope}"

    def document(self) -> dict[str, Any]:
        return {
            "function": self.function,
            "file": str(self.file),
            "sha256": self.source_sha256,
            "versions": {
                version: result.document() | ({"fault": self.faults[version]} if version in self.faults else {})
                for version, result in self.compares.items()
            },
            "best_percent": round(self.best_percent, 6),
            "exact": self.exact,
            "preconditions": list(self.preconditions),
            "seconds": round(self.seconds, 3),
            "compiler": self.compiler,
            **(
                {"required_versions": list(self.required_versions), "required_exact": self.required_exact}
                if self.required_versions is not None
                else {}
            ),
        }

    def lines(self) -> list[str]:
        output = [line for result in self.compares.values() for line in result.lines]
        output.extend(f"rule broken: {line}" for line in self.rule_lines)
        output.append(
            f"{self.function}: {'EXACT in every version' if self.exact else f'best {self.best_percent:.2f}%'}"
        )
        if self.required_versions is not None:
            output.append(
                f"required versions {', '.join(self.required_versions)}: "
                + ("EXACT" if self.required_exact else "not exact")
            )
        return output


def function_of(file: Path) -> str:
    if file.suffix != ".c" or not re.fullmatch(r"[A-Za-z_]\w*", file.stem):
        raise Held("compare", f"compare.file: {file}: expected FUNC.c")
    if not file.is_file():
        raise Held("compare", f"compare.file: {file}: missing file")
    return file.stem


def row_of(project: Project, function: str, version: str) -> split.Function:
    rows = [row for row in split.functions(project, version) if function in row.aliases]
    if len(rows) != 1:
        raise Held("compare", f"compare.row: {function}: expected one row in VERSION {version}, found {len(rows)}")
    return rows[0]


def published(project: Project, function: str) -> bool:
    """FUNC's row is C in every holding version: src/FUNC.c is what the build links, not an unmatched draft."""
    return all(row_of(project, function, v).kind == "c" for v in split.holding_versions(project, function))


def view_for(project: Project, file: Path, function: str) -> Project:
    """A file under build/work/FUNC/ sees that draft's own headers first."""
    from unbake.fold import apply as fold_apply

    if not file.resolve().is_relative_to(project.work.resolve()):
        return project
    fold_apply.link_private_includes(project, function)
    return draft_view(project, function)


def measure(
    project: Project,
    host: Host,
    file: Path,
    *,
    versions: tuple[str, ...] | None = None,
    retain_link_faults: bool = False,
) -> Compared:
    """Measure without recording an attempt. Compile failures retain native faults and nonexact placeholders.
    Link failures refuse by default; explicit scoped comparison retains them per version so no optional
    version's failure prevents measuring the required ones. A fault is never an exact comparison."""
    from unbake import runner
    from unbake.decomp import checks

    started = time.monotonic()
    function = function_of(file)
    selected = versions or split.holding_versions(project, function)
    view = view_for(project, file, function)
    content = file.read_bytes()
    broken = [finding for finding in checks.run(content.decode()) if finding.fakematch is None]
    preconditions = [checks.message(finding) for finding in broken]
    rule_lines = [checks.plain(finding) for finding in broken]
    results: dict[str, Compare] = {}
    faults: dict[str, dict[str, Any]] = {}
    for version in selected:
        row = row_of(project, function, version)
        target = split.words(project, row)
        compiling = True
        try:
            with runner.compile_unit(view, host, file, version, unit=function) as obj:
                compiling = False
                linked, problems = runner.link_function(project, host, obj, version, row, file)
        except Held as error:
            if not compiling and not retain_link_faults:
                raise
            faults[version] = process.fault(error)
            results[version] = Compare(
                version,
                0,
                len(target) // 4,
                dict.fromkeys(TYPES, 0) | {"changed": len(target) // 4},
                [f"VERSION {version}: {error.reason}"],
                0.0,
            )
            continue
        result = compare_words(version, target, linked)
        if problems:
            result.typed["relocation"] += len(problems)
            result.lines.extend(f"constant: {problem}" for problem in problems)
        results[version] = result
    digest = hashlib.sha256(content).hexdigest()
    compiler = view.compiler_reference(function)
    return Compared(
        function, file, digest, results, preconditions, time.monotonic() - started, compiler, rule_lines, faults
    )


def compare(project: Project, host: Host, file: Path, *, required_versions: tuple[str, ...] | None = None) -> Compared:
    """Measure every holding version (trying the other configured compilers when not exact) and record it."""
    from unbake.compilers import candidates

    required = None
    if required_versions is not None:
        function = function_of(file)
        holding = split.holding_versions(project, function)
        if (
            not required_versions
            or len(set(required_versions)) != len(required_versions)
            or set(required_versions) - set(holding)
        ):
            raise Held(
                "compare", f"compare.versions: {function}: require distinct holding versions from {', '.join(holding)}"
            )
        required = tuple(v for v in holding if v in required_versions or row_of(project, function, v).kind == "c")
    try:
        configured: Compared | Held = (
            measure(project, host, file) if required is None else measure(project, host, file, retain_link_faults=True)
        )
    except Held as error:
        configured = error
    _choice, chosen = (
        candidates.resolve(project, host, file, configured)
        if required is None
        else candidates.resolve(project, host, file, configured, required_versions=required)
    )
    assert isinstance(chosen, Compared)
    measured = chosen
    measured.required_versions = required
    first = row_of(project, measured.function, next(iter(measured.compares)))
    size = first.end - first.start
    attempts.append(
        project,
        attempts.Attempt(
            attempts.now(),
            measured.function,
            measured.source_sha256,
            size,
            measured.document()["versions"],
            measured.best_percent,
            measured.exact,
            measured.seconds,
            measured.compiler,
        ),
    )
    return measured
