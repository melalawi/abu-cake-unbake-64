"""Evidence-conditioned finite option episodes shared by compare and source search."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from unbake.compilers.recipe_options import PHASES, OptionSpec, ResolvedRecipe, UnitRecipe


@dataclass(frozen=True)
class Capability:
    state: str
    recipe_digest: str
    stage: str
    argv: tuple[str, ...] = ()
    native: dict[str, Any] | None = None
    reason: str | None = None

    def document(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class OptionEvidence:
    kind: str
    artifact_digest: str
    refs: tuple[str, ...]
    options: tuple[str, ...] = ()
    compatible: bool = False


@dataclass(frozen=True)
class Episode:
    recipes: tuple[UnitRecipe, ...]
    parent_limit: int
    evidence: tuple[OptionEvidence, ...]
    stop_reason: str = "finite_plan"


def option_space(spec: Any) -> tuple[OptionSpec, ...]:
    """Release data, not a compiler-name branch in the planner."""
    return (
        OptionSpec(
            "small_data",
            "compile",
            ("-G{value}",),
            tuple(spec.small_data),
            "small-data",
            semantic_risks=("GP/address-cost and placement",),
        ),
        OptionSpec("optimization", "compile", ("-O{value}",), (0, 1, 2, 3), "optimization"),
        OptionSpec("debug", "compile", ("-g{value}",), (0, 1, 2, 3), "debug"),
        OptionSpec(
            "isa",
            "compile",
            ("-mips{value}",),
            tuple(spec.isa),
            "isa",
            prerequisites=("target instruction-set compatibility",),
        ),
        OptionSpec("assembler_small_data", "assemble", ("-G{value}",), (0,), "small-data"),
        *(
            OptionSpec(token.removeprefix("-"), "compile", (token,), evidence_refs=("registry pinned probe",))
            for token in spec.supported_options
            if token.startswith("-f")
        ),
    )


def validate_options(recipe: ResolvedRecipe, spec: Any, accepts: Any, baseline: tuple[str, ...] | None = None) -> None:
    import re

    space = option_space(spec)
    for phase in PHASES:
        iterator = iter(recipe.phase(phase))
        for token in iterator:
            if phase == "preprocess":
                from unbake.compilers.recipe_options import PAIRS

                if token in PAIRS:
                    if next(iterator, None) is None:
                        raise ValueError(f"recipe.options.preprocess: {token}: missing argument")
                elif not token.startswith(("-I", "-D", "-U")):
                    raise ValueError(f"recipe.options.preprocess: {token}: unsupported phase")
                continue
            if phase == "link":
                raise ValueError(
                    "recipe.options.link: arbitrary link options are not supported by the bounded placement owner"
                )
            if token.startswith("-d"):
                raise ValueError("recipe.options.compile: diagnostic-only switches cannot enter a build recipe")
            allowed = spec.cflags if phase == "compile" else ()
            option = next(
                (
                    o
                    for o in space
                    if o.phase == phase
                    and any(re.fullmatch(re.escape(t).replace(re.escape("{value}"), "(.+)"), token) for t in o.tokens)
                ),
                None,
            )
            if option is not None:
                if option.values and token not in {t.format(value=v) for t in option.tokens for v in option.values}:
                    raise ValueError(f"recipe.options.{phase}: {token}: outside pinned release values")
            elif phase == "assemble":
                # Assembler settings are independent, finite driver baseline.
                if token not in (
                    "-EB",
                    "-mips1",
                    "-mips2",
                    "-mips3",
                    "-mips4",
                    "-mgp32",
                    "-mfp32",
                    "-mfp64",
                    "-march=vr4300",
                    "-mabi=32",
                    "--no-pad-sections",
                ):
                    raise ValueError(f"recipe.options.assemble: {token}: unsupported phase")
            elif token not in allowed and token not in spec.supported_options and not accepts(token):
                raise ValueError(f"recipe.options.compile: {token}: tool policy refused")


def admit_trial(project: Any, unit: str, recipe: UnitRecipe) -> None:
    """Speculative recipes preserve the TU's declared effective ABI and ISA."""
    from dataclasses import replace

    from unbake.compilers.drivers import resolved
    from unbake.compilers.recipe_options import option_group
    from unbake.config import Held
    from unbake.process import named

    view = replace(project, units={**project.units, project.unit_path(unit): recipe})
    for version in project.versions:
        before, after = resolved(project, version, unit), resolved(view, version, unit)
        for group in ("gp-width", "fp-width", "float-abi", "isa"):
            old = tuple(t for t in before.phase("compile") if option_group(t, "compile", ()) == group)
            new = tuple(t for t in after.phase("compile") if option_group(t, "compile", ()) == group)
            if old != new:
                raise Held(
                    named(
                        "recipe.compatibility",
                        f"{unit}: trial changes declared {group}; compatible target/caller proof required",
                        owner="compilers.options",
                        stage="options",
                        evidence={"baseline": list(old), "trial": list(new)},
                    )
                )


def plan_episode(
    project: Any, unit: str, *, evidence: tuple[OptionEvidence, ...] = (), recipe_limit: int = 8, parent_limit: int = 4
) -> Episode:
    from dataclasses import replace

    from unbake.compilers.drivers import resolved
    from unbake.compilers.families import family_for
    from unbake.compilers.registry import specification

    if not recipe_limit >= 1 or not parent_limit >= 1:
        raise ValueError("option.episode: explicit positive recipe and parent limits required")
    recipe_limit, parent_limit = min(recipe_limit, 8), min(parent_limit, 4)
    baseline = project.recipe_for(unit)
    spec = specification(baseline.compiler)
    space = family_for(baseline.compiler).option_space(spec)
    deltas: list[tuple[str, ...]] = []
    # Compatible retained recipes and measured address evidence precede unrelated syntax.
    for row in evidence:
        if row.compatible and row.artifact_digest and row.refs and row.options:
            deltas.append(row.options)
    for option in space:
        if option.id == "small_data":
            deltas.extend((f"-G{value}",) for value in option.values)
    deltas.extend(
        (token,)
        for token in ("-fno-cse-follow-jumps", "-fno-cse-skip-blocks", "-fno-thread-jumps", "-fno-rerun-cse-after-loop")
        if token in spec.supported_options
    )
    deltas.extend((f"-O{value}",) for value in (1, 3))
    recipes = [baseline]

    def effective(recipe: UnitRecipe) -> tuple[str, ...]:
        view = replace(project, units={**project.units, project.unit_path(unit): recipe})
        return tuple(resolved(view, version, unit).digest for version in project.versions)

    seen = {effective(baseline)}
    from unbake.compilers.recipe_options import merge_options

    for delta in deltas:
        phases = dict(baseline.options)
        phases["compile"] = merge_options(phases["compile"], delta)
        recipe = UnitRecipe(baseline.compiler, tuple((p, phases[p]) for p in PHASES), baseline.functions)
        from unbake.config import Held

        try:
            admit_trial(project, unit, recipe)
            digest = effective(recipe)
        except Held:
            # Static release/ABI/phase refusal is retained by the executor when
            # explicitly requested; it is not an eligible speculative recipe.
            continue
        if digest not in seen:
            seen.add(digest)
            recipes.append(recipe)
        if len(recipes) >= recipe_limit:
            break
    return Episode(tuple(recipes), parent_limit, evidence)


def infer_options(facts: dict[str, Any]) -> tuple[OptionEvidence, ...]:
    """Labels/names cannot establish compiler support or successful native bytes."""
    rows = []
    values = [*facts.get("option_evidence", ())]
    for holder in facts.values():
        if isinstance(holder, dict):
            values.extend(holder.get("option_evidence", ()))
    for value in values:
        if value.get("artifact_digest") and value.get("refs"):
            rows.append(
                OptionEvidence(
                    value["kind"],
                    value["artifact_digest"],
                    tuple(value["refs"]),
                    tuple(value.get("options", ())),
                    bool(value.get("compatible")),
                )
            )
    return tuple(sorted(rows, key=lambda row: (row.kind, row.artifact_digest, row.options)))


def classify_capability(recipe_digest: str, measurement: Any = None, fault: dict[str, Any] | None = None) -> Capability:
    """First owning stage and native result distinguish refusal from measured bytes."""
    if fault is not None:
        from unbake.process import Fault, native_results

        error = Fault.read(fault)
        results = native_results(error)
        native = results[0] if results else None
        if native is None:
            state = "tool_refused"
        elif native.category in ("native-os", "native-signal"):
            state = "unavailable"
        elif error.cause.stage in ("compile", "preprocess") or native.context.get("stage") in ("compile", "preprocess"):
            state = "compiler_refused"
        else:
            state = "unavailable"
        return Capability(
            state, recipe_digest, error.cause.stage, native.args if native else (), fault, error.cause.reason
        )
    if measurement is None or not measurement.available or not measurement.strict.get("available"):
        return Capability("unavailable", recipe_digest, "linked", reason="complete native measurement unavailable")
    return Capability(
        "measured_exact" if measurement.exact else "measured_nonexact",
        recipe_digest,
        "linked",
        native=measurement.document(),
    )
