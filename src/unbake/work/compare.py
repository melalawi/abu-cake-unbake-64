"""compare: compile one file for every holding version, link it alone at its address and compare with the ROM."""

from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from unbake.config import Held, Host, Project, draft_view
from unbake.inputs import DependencySet
from unbake.layout import split
from unbake.process import Fault, capture
from unbake.process import named as cause_named
from unbake.work import attempts
from unbake.work.score import Measurement, measure_words, unavailable


@dataclass
class Compared:
    function: str
    file: Path
    source_sha256: str
    compares: dict[str, Measurement]
    preconditions: list[str] = field(default_factory=list)
    seconds: float = 0.0
    compiler: str = ""
    # The preconditions in plain words (the raw messages stay in preconditions for the JSON document).
    rule_lines: list[str] = field(default_factory=list)
    faults: dict[str, dict[str, Any]] = field(default_factory=dict)
    required_versions: tuple[str, ...] | None = None
    facts: dict[str, dict[str, Any]] = field(default_factory=dict)

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
    def best_percent(self) -> float | None:
        return (
            min((result.percent for result in self.compares.values() if result.percent is not None), default=None)
            if self.compares and all(result.available for result in self.compares.values())
            else None
        )

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
            "best_percent": round(self.best_percent, 6) if self.best_percent is not None else None,
            "exact": self.exact,
            "preconditions": list(self.preconditions),
            "seconds": round(self.seconds, 3),
            "compiler": self.compiler,
            **({"facts": self.facts} if self.facts else {}),
            **(
                {"required_versions": list(self.required_versions), "required_exact": self.required_exact}
                if self.required_versions is not None
                else {}
            ),
        }

    def lines(self) -> list[str]:
        output = [line for result in self.compares.values() for line in result.lines]
        from unbake.work.compare_facts import lines

        output.extend(line for version, facts in self.facts.items() for line in lines(version, facts))
        output.extend(f"rule broken: {line}" for line in self.rule_lines)
        state = (
            "EXACT in every version"
            if self.exact
            else f"best {self.best_percent:.2f}%"
            if self.best_percent is not None
            else "measurement unavailable"
        )
        output.append(f"{self.function}: {state}")
        if self.required_versions is not None:
            output.append(
                f"required versions {', '.join(self.required_versions)}: "
                + ("EXACT" if self.required_exact else "not exact")
            )
        return output


def function_of(file: Path) -> str:
    function = file.stem.removesuffix(".best")
    if file.suffix != ".c" or not re.fullmatch(r"[A-Za-z_]\w*", function):
        raise Held(
            cause_named(
                "compare.file",
                f"compare.file: {file}: expected FUNC.c or FUNC.best.c",
                owner="work.compare",
                stage="compare",
            )
        )
    if not file.is_file():
        raise Held(
            cause_named("compare.file", f"compare.file: {file}: missing file", owner="work.compare", stage="compare")
        )
    return function


def row_of(project: Project, function: str, version: str) -> split.Function:
    rows = [row for row in split.functions(project, version) if function in row.aliases]
    if len(rows) != 1:
        raise Held(
            cause_named(
                "compare.row",
                f"compare.row: {function}: expected one row in VERSION {version}, found {len(rows)}",
                owner="work.compare",
                stage="compare",
            )
        )
    return rows[0]


def published(project: Project, function: str) -> bool:
    """FUNC's row is C in every holding version: src/FUNC.c is what the build links, not an unmatched draft."""
    return all(row_of(project, function, v).kind == "c" for v in split.holding_versions(project, function))


def view_for(project: Project, file: Path, function: str) -> Project:
    """Keep an explicit header view; otherwise give a work file its draft headers."""
    from unbake.fold import apply as fold_apply

    if project.work_include or not file.resolve().is_relative_to(project.work.resolve()):
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
    non_matching = (
        file.resolve() == (project.src / f"{function}.c").resolve()
        and attempts.ledger(project).fuzzy(function) is not None
    )
    broken = [finding for finding in checks.run(content.decode()) if finding.fakematch is None]
    preconditions = [checks.message(finding) for finding in broken]
    rule_lines = [checks.plain(finding) for finding in broken]
    results: dict[str, Measurement] = {}
    faults: dict[str, dict[str, Any]] = {}
    from unbake.fold import provider_reuse, self_prototype
    from unbake.typemap import namespace

    with (
        self_prototype.view(view, host, file, content.decode()) as view,
        provider_reuse.view(view, host, tuple(selected)) as view,
        namespace.comparison_view(view, host, file, content.decode()) as (view, candidate),
    ):
        for version in selected:
            row = row_of(project, function, version)
            target = split.words(project, row)
            compiling = True
            try:
                options: dict[str, Any] = {"non_matching": True} if non_matching else {}
                with runner.compile_unit(view, host, candidate, version, unit=function, **options) as obj:
                    compiling = False
                    linked, problems = runner.link_function(project, host, obj, version, row, file)
            except Held as error:
                if not compiling and not retain_link_faults:
                    raise
                faults[version] = capture(
                    error, cause=cause_named("work.compare.unexpected", str(error), owner="work.compare", stage="work")
                ).document()
                results[version] = unavailable(version, len(target) // 4, error.fault)
                results[version].lines.append(f"VERSION {version}: {error.reason}")
                continue
            result = measure_words(version, target, linked)
            if problems:
                assert result.typed is not None
                result.typed["relocation"] += len(problems)
                result.lines.extend(f"constant: {problem}" for problem in problems)
            results[version] = result
    digest = hashlib.sha256(content).hexdigest()
    compiler = view.compiler_reference(function)
    return Compared(
        function, file, digest, results, preconditions, time.monotonic() - started, compiler, rule_lines, faults
    )


def _compare(
    project: Project,
    host: Host,
    file: Path,
    *,
    required_versions: tuple[str, ...] | None = None,
    explain_schedule: bool = False,
) -> Compared:
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
                cause_named(
                    "compare.versions",
                    f"compare.versions: {function}: require distinct holding versions from {', '.join(holding)}",
                    owner="work.compare",
                    stage="compare",
                )
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
    from unbake.work.compare_facts import attach

    attach(project, measured)
    if explain_schedule:
        from unbake.work.compare_dump import collect

        collect(project, host, measured)
    return measured


def operation_dependencies(project: Project, host: Host, file: Path) -> DependencySet:
    from unbake import cache, inputs
    from unbake.project.headers import Graph

    graph = Graph.capture(project)
    closure = graph.closure((file,))
    dependencies = closure.dependency_set
    modules = (
        "work/compare.py",
        "work/compare_facts.py",
        "work/compare_dump.py",
        "runner.py",
        "process.py",
        "compilers/drivers.py",
        "compilers/candidates.py",
        "compilers/families/gcc/dump_facts.py",
        "compilers/families/gcc/__init__.py",
        "compilers/families/ido/__init__.py",
        "compilers/families/mips.py",
    )
    tool = Path(__file__).parents[1]
    recipe = cache.key(*(inputs.digest(tool / name, algorithm="sha256", reuse=cache.configured()) for name in modules))
    native = {
        field: inputs.digest(Path(getattr(host, field)), algorithm="sha256", reuse=cache.configured())
        for field in ("cpp", "mips_as", "mips_ld", "mips_objcopy", "n64link")
    }
    native.update(
        {
            "cc:" + name: inputs.digest(compiler.cc, algorithm="sha256", reuse=cache.configured())
            for name, compiler in project.compilers.items()
        }
    )
    paths = {
        project.root / "config.toml",
        project.root / "layout.toml",
        *(
            p
            for version in project.versions
            for p in (project.version(version).split, project.version(version).symbols)
        ),
    }
    paths.update(
        path for version in project.versions for path in project.build_link(version).glob("*") if path.is_file()
    )
    pins = {pin.path: pin for pin in dependencies.files}
    for path in paths:
        pin = inputs.file_pin(path, root=project.root, root_id="project", reuse=cache.configured())
        pins[pin.path] = pin
    return inputs.DependencySet(
        tuple(pins.values()),
        {
            **dependencies.values,
            "compiler": project.compiler_reference(file.stem),
            "versions": list(project.versions),
            "memory_worker_bytes": host.memory_worker_bytes,
            "cache_memory_bytes": host.cache_memory_bytes,
            "native": native,
            "native_flags": {
                "compiler": list(project.compiler_for(file.stem).cflags),
                "as": list(project.asflags),
                "gnu_as": list(project.gnu_asflags),
                "cpp": list(project.cppflags),
            },
            "source_sha256": inputs.digest(file, algorithm="sha256", reuse=cache.configured()),
            "dependencies_unknown": closure.unknown,
        },
        {**dependencies.recipes, "compare": recipe},
    )


def compare(
    project: Project,
    host: Host,
    file: Path,
    *,
    required_versions: tuple[str, ...] | None = None,
    explain_schedule: bool = False,
) -> Compared:
    if attempts.producer_operation() is not None:
        return _compare(project, host, file, required_versions=required_versions, explain_schedule=explain_schedule)
    function = function_of(file)
    from unbake.work.attempts import RetryScope, command_ledger

    with (
        command_ledger(project),
        RetryScope(
            project,
            "compare",
            function,
            {
                "path": str(file.relative_to(project.root)) if file.is_relative_to(project.root) else file.name,
                "required_versions": list(required_versions) if required_versions is not None else None,
                "explain_schedule": explain_schedule,
            },
            operation_dependencies(project, host, file),
        ) as scope,
    ):
        result = _compare(project, host, file, required_versions=required_versions, explain_schedule=explain_schedule)
        first = row_of(project, result.function, next(iter(result.compares)))
        observed = attempts.Attempt(
            attempts.now(),
            result.function,
            result.source_sha256,
            first.end - first.start,
            result.document()["versions"],
            result.best_percent,
            result.exact,
            result.seconds,
            result.compiler,
        )
        scope.value = {**result.document(), "attempt": observed.document()}
        scope.fault = Fault.read(next(iter(result.faults.values()))) if result.faults else None
        return result
