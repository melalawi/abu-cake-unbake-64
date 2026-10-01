"""Explicit draft compiler flag probes and per-VERSION rankings."""

from __future__ import annotations

import shlex
import tomllib
from dataclasses import dataclass, replace
from pathlib import Path

from unbake.decomp.candidate_ranking import measured_candidate_rank
from unbake.decomp.trial_compare import Compare
from unbake.project import toolchain
from unbake.project.config import Held, Project


@dataclass
class FlagResult:
    flags: tuple[str, ...]
    compares: dict[str, Compare]
    failures: dict[str, str]


def compiler_variants(project: Project, source: Path) -> list[tuple[str, ...]]:
    """Require the source compiler's explicit registry probes, including its baseline."""
    ident = project.compiler_for(source).id
    path = toolchain.REGISTRY_PATH
    label = f"{path} [compilers.{ident}].flag_variants"
    try:
        with path.open("rb") as stream:
            values = tomllib.load(stream)["compilers"][ident]["flag_variants"]
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise Held("try", f"{label}: explicit array of flag arrays required: {error}") from error
    if not isinstance(values, list) or any(
        not isinstance(row, list) or any(not isinstance(flag, str) or not flag.strip() for flag in row)
        for row in values
    ):
        raise Held("try", f"{label}: expected array of flag arrays")
    variants = [tuple(row) for row in values]
    if not variants or variants[0] != () or len(set(variants)) != len(variants):
        raise Held("try", f"{label}: first variant must be [] (project flags); variants must be unique")
    return variants


def variant_project(project: Project, source: Path, flags: tuple[str, ...]) -> Project:
    compiler = project.compiler_for(source)
    return replace(
        project,
        compilers={**project.compilers, compiler.id: replace(compiler, cflags=(*compiler.cflags, *flags))},
    )


def ranking(results: list[FlagResult], versions: list[str]) -> list[str]:
    lines = ["flag probe: variants extend project cflags; project unit_cflags remain in effect"]
    for version in versions:

        def key(result: FlagResult, version: str = version) -> tuple[bool, tuple[bool, int, int, float]]:
            comparison = result.compares.get(version)
            if comparison is None or version in result.failures:
                return True, (True, 0, 0, 0.0)
            return False, measured_candidate_rank({version: comparison})

        baseline = key(results[0])
        ordered = sorted(
            results,
            key=key,
        )
        for place, result in enumerate(ordered, 1):
            label = shlex.join(result.flags) if result.flags else "baseline (project flags)"
            if version in result.failures:
                lines.append(f"VERSION {version} flags {place}: {label}; compile failed: {result.failures[version]}")
                continue
            score = result.compares[version].match_percent
            marker = "; BEATS PROJECT FLAGS" if key(result) < baseline else ""
            lines.append(f"VERSION {version} flags {place}: {label}; objdiff {score:.6f}%{marker}")
    return lines
