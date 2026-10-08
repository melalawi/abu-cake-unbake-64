"""compare: compile one file for every holding version, link it alone at its address and compare with the ROM."""

from __future__ import annotations

import hashlib
import re
import time
from collections.abc import Callable
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
    project_root: Path | None = None
    unit_recipe: dict[str, Any] | None = None
    option_episode: dict[str, Any] | None = None
    next_action: str = ""

    def blockers(self) -> list[str]:
        """Source-rule sentences that keep this text from landing, volatile first."""
        return sorted(self.rule_lines, key=lambda line: "volatile storage" not in line)

    @property
    def landable(self) -> bool:
        return not self.preconditions

    @property
    def required_exact(self) -> bool:
        return acceptance(self.compares, self.required_versions, self.preconditions)

    @property
    def identical_everywhere(self) -> bool:
        return acceptance(self.compares, tuple(self.compares), self.preconditions)

    @property
    def exact(self) -> bool:
        return self.identical_everywhere

    @property
    def best_percent(self) -> float | None:
        return (
            min((result.percent for result in self.compares.values() if result.percent is not None), default=None)
            if self.compares and all(result.available for result in self.compares.values())
            else None
        )

    @property
    def next_command(self) -> str:
        if self.next_action:
            return self.next_action
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
            "landable": self.landable,
            "landable_blockers": self.blockers(),
            "preconditions": list(self.preconditions),
            "seconds": round(self.seconds, 3),
            "compiler": self.compiler,
            "recipe": self.unit_recipe,
            "option_episode": self.option_episode,
            "next_command": self.next_command,
            **({"facts": self.facts} if self.facts else {}),
            **(
                {"required_versions": list(self.required_versions), "required_exact": self.required_exact}
                if self.required_versions is not None
                else {}
            ),
        }

    def lines(self) -> list[str]:
        from unbake.work.hints import technique_lines

        output = [f"NOT LANDABLE: {line}" for line in self.blockers()]
        output.extend(line for result in self.compares.values() for line in result.lines)
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
        if self.option_episode is not None:
            output.extend(technique_lines(tuple(self.option_episode.get("hints", ()))))
            output.append(f"option stop: {self.option_episode['stop_reason']}")
        if self.required_versions is not None:
            output.append(
                f"required versions {', '.join(self.required_versions)}: "
                + ("EXACT" if self.required_exact else "not exact")
            )
        return output


def acceptance(
    compares: dict[str, Measurement], required: tuple[str, ...] | None, preconditions: list[str] | tuple[str, ...] = ()
) -> bool:
    versions = tuple(compares) if required is None else required
    return (
        bool(versions)
        and len(set(versions)) == len(versions)
        and not preconditions
        and all(version in compares and compares[version].exact for version in versions)
    )


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
    from unbake.decomp import field_access

    field_access.restore(project, function, file.read_text())
    fold_apply.link_private_includes(project, function)
    return draft_view(project, function)


def measure(
    project: Project,
    host: Host,
    file: Path,
    *,
    versions: tuple[str, ...] | None = None,
    retain_link_faults: bool = False,
    score_cache: dict[str, Measurement] | None = None,
    on_linked: Callable[[str, Path, bytes, list[str], dict[str, Any]], None] | None = None,
) -> Compared:
    """Measure without recording an attempt. Compile failures retain native faults and nonexact placeholders.
    Link failures refuse by default; explicit scoped comparison retains them per version so no optional
    version's failure prevents measuring the required ones. A fault is never an exact comparison."""
    from unbake import runner, tui
    from unbake.decomp import checks

    started = time.monotonic()
    from unbake.work.source_scope import admit_source

    function = admit_source(project, file).subject
    selected = versions or split.holding_versions(project, function)
    view = view_for(project, file, function)
    content = file.read_bytes()
    input_view = view
    with tui.task("Reading dependencies"):
        dependency_before = operation_dependencies(input_view, host, file)
    non_matching = (
        file.resolve() == (project.src / f"{function}.c").resolve()
        and attempts.ledger(project).fuzzy(function) is not None
    )
    expanded = None
    if selected:
        try:
            expanded = runner.preprocess(project, host, file, selected[0], unit=function)
        except Held:
            expanded = None  # the compile below reports the same failure with its cause
    broken = [finding for finding in checks.run(content.decode(), expanded, function) if finding.fakematch is None]
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
            capture_info: dict[str, Any] = {}
            try:
                options: dict[str, Any] = {"non_matching": True} if non_matching else {}
                with runner.compile_unit(
                    view, host, candidate, version, unit=function, capture_info=capture_info, **options
                ) as obj:
                    compiling = False
                    linked, problems = runner.link_function(
                        project, host, obj, version, row, file, capture_info=capture_info
                    )
                    if on_linked is not None:
                        on_linked(version, obj, linked, problems, capture_info)
            except Held as error:
                if not compiling and not retain_link_faults:
                    raise
                faults[version] = capture(
                    error, cause=cause_named("work.compare.unexpected", str(error), owner="work.compare", stage="work")
                ).document()
                results[version] = unavailable(version, len(target) // 4, error.fault)
                results[version].lines.append(f"VERSION {version}: {error.reason}")
                continue
            import copy

            from unbake.compilers.drivers import resolved
            from unbake.compilers.recipe_options import recipe_digest

            recipe = resolved(view, version, function)
            equivalence = recipe_digest(
                {
                    "linked_sha256": hashlib.sha256(linked).hexdigest(),
                    "relocations": capture_info.get("relocations"),
                    "literals": capture_info.get("literal_layout"),
                    "recipe": recipe.digest,
                    "placement": capture_info.get("placement"),
                    "target_sha256": hashlib.sha256(target).hexdigest(),
                    "refusals": problems,
                }
            )
            if score_cache is not None:
                from unbake import effort

                effort.count("memo.compare.score", int(equivalence in score_cache), 1)
            if score_cache is not None and equivalence in score_cache:
                result = copy.deepcopy(score_cache[equivalence])
            else:
                result = measure_words(version, target, linked)
                if score_cache is not None:
                    score_cache[equivalence] = copy.deepcopy(result)
            from unbake.compilers.drivers import resolved

            result.provenance = {
                **capture_info,
                "source_sha256": hashlib.sha256(content).hexdigest(),
                "recipe": resolved(view, version, function).document(),
                "recipe_digest": resolved(view, version, function).digest,
                "required_versions": list(selected),
                "target_sha256": hashlib.sha256(target).hexdigest(),
                "dependencies": dependency_before.document(),
                "dependency_digest": dependency_before.digest,
                "dependency_current": False,
                "unit_recipe": view.recipe_for(function).document(),
            }
            if not {"preprocessed_sha256", "object_sha256", "placed_object_sha256"} <= capture_info.keys():
                result.strict = {"available": False, "reason": "native digest chain incomplete"}
            if problems:
                assert result.typed is not None
                result.typed["relocation"] += len(problems)
                result.lines.extend(f"constant: {problem}" for problem in problems)
            results[version] = result
    # One readback after every version compiled: the closure is the same for each, so a per-version readback
    # only repeated the whole header scan.
    if any(result.provenance for result in results.values()):
        with tui.task("Reading dependencies back"):
            current = operation_dependencies(input_view, host, file) == dependency_before
        for result in results.values():
            if result.provenance:
                result.provenance["dependency_current"] = current
    digest = hashlib.sha256(content).hexdigest()
    compiler = view.compiler_reference(function)
    compared = Compared(
        function, file, digest, results, preconditions, time.monotonic() - started, compiler, rule_lines, faults
    )
    compared.project_root, compared.unit_recipe = project.root, view.recipe_for(function).document()
    return compared


def _compare(
    project: Project,
    host: Host,
    file: Path,
    *,
    required_versions: tuple[str, ...] | None = None,
    explain_schedule: bool = False,
    flags: bool = False,
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
    from unbake.work.compare_facts import attach

    attach(project, chosen)
    if flags:
        from dataclasses import replace

        from unbake.compilers.recipe_options import UnitRecipe
        from unbake.search.pairs import option_episode

        chosen_recipe = UnitRecipe.read(chosen.unit_recipe)
        option_view = replace(project, units={**project.units, project.unit_path(chosen.function): chosen_recipe})
        measured = option_episode(option_view, host, file, chosen)
    measured.required_versions = required
    from unbake.work.source_scope import admit_source

    measured.next_action = admit_source(project, file).saved_action(
        file, exact=measured.required_exact, required_versions=required or ()
    )
    from unbake.work.compare_facts import attach

    attach(project, measured)
    if explain_schedule:
        from unbake.work.compare_dump import collect

        collect(project, host, measured)
    return measured


def dependency_state(project: Project, host: Host, file: Path) -> str:
    """Identity of every file and flag operation_dependencies reads: stat signatures, no content read.
    The tool's own writes change a signature, so they alone invalidate a retained answer."""
    from unbake import cache, inputs
    from unbake.compilers import drivers

    function = function_of(file)
    holding = split.holding_versions(project, function)
    roots = {*project.include}
    flags = {version: drivers.flags(project, version, function) for version in holding}
    for command in flags.values():
        roots.update(project.root / flag[2:] for flag in command if flag.startswith("-I") and len(flag) > 2)
    paths = {file, project.root / "config.toml", project.root / "layout.toml"}
    for root in roots:
        paths.update(root.rglob("*.h"))
    paths.update(project.src.rglob("*.c"))
    paths.update(project.src.rglob("*.h"))
    for version in project.versions:
        paths.update((project.version(version).split, project.version(version).symbols))
        paths.update(path for path in project.build_link(version).glob("*"))
    paths.update(
        project.tools / name
        for name in ("compilers.sha256", "compiler-driver.sha256", "compiler_contracts.py", "recipe_options.py")
    )
    paths.update(Path(getattr(host, field)) for field in ("cpp", "mips_as", "mips_ld", "mips_objcopy", "n64link"))
    paths.update(compiler.cc for compiler in project.compilers.values())

    def stat(path: Path) -> object:
        try:
            return inputs.signature(path)
        except OSError:
            return None

    state = (
        project.id,
        str(project.root),
        holding,
        flags,
        project.versions,
        project.compiler_reference(function),
        project.recipe_for(function).document(),
        host.memory_worker_bytes,
        host.cache_memory_bytes,
        sorted((str(path), stat(path)) for path in paths),
    )
    return cache.key(repr(state))


def operation_dependencies(project: Project, host: Host, file: Path) -> DependencySet:
    """The dependencies of measuring FILE, read once per tree state: later calls in the command reuse them."""
    from unbake import cache

    if not cache.configured():
        return _operation_dependencies(project, host, file)
    return cache.memo(
        "work.operation_dependencies",
        dependency_state(project, host, file),
        lambda: _operation_dependencies(project, host, file),
        size=cache.memory_size,
        copy_out=cache.clone,
    )


def _operation_dependencies(project: Project, host: Host, file: Path) -> DependencySet:
    from unbake import cache, inputs
    from unbake.project.headers import Graph

    graph = Graph.capture(project)
    from unbake.compilers import drivers

    function = function_of(file)
    from unbake.layout import split

    closures = [
        graph.closure((file,), drivers.flags(project, version, function))
        for version in split.holding_versions(project, function)
    ]
    dependencies = DependencySet(
        tuple({pin.path: pin for closure in closures for pin in closure.dependency_set.files}.values()),
        {
            "holders": {
                v: c.dependency_set.document()
                for v, c in zip(split.holding_versions(project, function), closures, strict=True)
            }
        },
        {k: value for closure in closures for k, value in closure.dependency_set.recipes.items()},
    )
    modules = (
        "work/compare.py",
        "fold/self_prototype.py",
        "fold/provider_reuse.py",
        "typemap/namespace.py",
        "work/compare_facts.py",
        "work/compare_dump.py",
        "runner.py",
        "process.py",
        "compilers/drivers.py",
        "compilers/recipe_options.py",
        "compilers/compiler_contracts.py",
        "compilers/options.py",
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
    paths.update(
        p
        for p in (
            project.tools / "compilers.sha256",
            project.tools / "compiler-driver.sha256",
            project.tools / "compiler_contracts.py",
            project.tools / "recipe_options.py",
        )
        if p.is_file()
    )
    pins = {pin.path: pin for pin in dependencies.files}
    for path in paths:
        pin = inputs.file_pin(path, root=project.root, root_id="project", reuse=cache.configured())
        pins[pin.path] = pin
    return inputs.DependencySet(
        tuple(pins.values()),
        {
            **dependencies.values,
            "compiler": project.compiler_reference(function),
            "versions": list(project.versions),
            "memory_worker_bytes": host.memory_worker_bytes,
            "cache_memory_bytes": host.cache_memory_bytes,
            "native": native,
            "native_flags": {
                "recipe": project.recipe_for(function).document(),
                "compiler": list(project.compiler_for(function).cflags),
                "as": list(project.asflags),
                "gnu_as": list(project.gnu_asflags),
                "cpp": list(project.cppflags),
            },
            "source_sha256": inputs.digest(file, algorithm="sha256", reuse=cache.configured()),
            "dependencies_unknown": any(closure.unknown for closure in closures),
            **(
                {"unresolved_includes": sorted({line for closure in closures for line in closure.unresolved})}
                if any(closure.unresolved for closure in closures)
                else {}
            ),
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
    flags: bool = False,
) -> Compared:
    if attempts.producer_operation() is not None:
        return _compare(
            project, host, file, required_versions=required_versions, explain_schedule=explain_schedule, flags=flags
        )
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
                "flags": flags,
            },
            operation_dependencies(project, host, file),
        ) as scope,
    ):
        result = _compare(
            project, host, file, required_versions=required_versions, explain_schedule=explain_schedule, flags=flags
        )
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
