"""Explicit unresolved compiler sets and evidence-backed regional pins."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import toml  # type: ignore[import-untyped]

from unbake.project import compiler_files
from unbake.project.config import Held, Project


def read(value: object, compilers: dict[str, Any]) -> dict[str, tuple[str, ...]]:
    if not isinstance(value, dict):
        raise Held("config", "compiler.tied_set: expected table")
    result = {}
    for ref, ids in value.items():
        if (
            not isinstance(ref, str)
            or not ref.startswith("tie:")
            or len(ref) <= 4
            or not isinstance(ids, list)
            or len(ids) < 2
            or any(not isinstance(ident, str) or ident not in compilers for ident in ids)
            or len(set(ids)) != len(ids)
        ):
            raise Held("config", f"compiler.tied_set: {ref}: expected at least two distinct configured compiler IDs")
        result[ref] = tuple(sorted(ids))
    return result


def reference(project: Project, source: Path) -> str | None:
    ident = project.compiler_reference(source)
    return ident if ident in project.compiler_ties else None


def candidate(project: Project, ref: str, ident: str) -> Project:
    if ident not in project.compiler_ties[ref]:
        raise Held("try", f"compiler.tied_set: {ref}: {ident} is not a member")
    return replace(
        project,
        units={name: ident if value == ref else value for name, value in project.units.items()},
        default_compiler=ident if project.default_compiler == ref else project.default_compiler,
    )


def pin(project: Project, ref: str, ident: str, evidence: dict[str, Any], config_sha256: str) -> Project:
    from unbake.project import build, config, makefile

    with build.lock(project):
        path = project.root / "config.toml"
        content = path.read_bytes()
        if hashlib.sha256(content).hexdigest() != config_sha256:
            raise Held("try", "compiler.tie_stale: configuration changed during candidate comparisons")
        data = toml.loads(content.decode())
        ids = read(data.get("compiler_ties", {}), project.compilers)
        if ids.get(ref) != project.compiler_ties[ref] or ident not in ids[ref]:
            raise Held("try", f"compiler.tied_set: {ref}: candidates changed during comparison")
        data["units"] = {name: ident if value == ref else value for name, value in data["units"].items()}
        if data["project"]["default_compiler"] == ref:
            data["project"]["default_compiler"] = ident
        data.setdefault("compiler_selections", {})[ref] = {
            "compiler": ident,
            "evidence_json": json.dumps(evidence, sort_keys=True),
        }
        recipe = project.tools / "build.json"
        try:
            previous_recipe = recipe.read_bytes()
        except OSError as error:
            raise Held("try", f"compiler.tie_recipe: rendered build recipe missing: {error}") from error
        manifest = project.tools / "compiler.sha256"
        try:
            previous_manifest = manifest.read_bytes()
            lines = previous_manifest.decode().splitlines(keepends=True)
        except (OSError, UnicodeError) as error:
            raise Held("try", f"compiler.tie_recipe: helper manifest missing or malformed: {error}") from error
        relative = recipe.relative_to(project.root).as_posix()
        entries = [i for i, line in enumerate(lines) if line.strip().split(maxsplit=1)[1:] == [relative]]
        if len(entries) != 1:
            raise Held("try", f"compiler.tie_recipe: {relative}: expected exactly one helper manifest entry")
        if lines[entries[0]].split()[0] != hashlib.sha256(previous_recipe).hexdigest():
            raise Held("try", f"compiler.tie_recipe: {relative}: existing recipe differs from helper manifest")
        rendered = (
            json.dumps(makefile.description(candidate(project, ref, ident)), sort_keys=True, indent=2) + "\n"
        ).encode()
        lines[entries[0]] = f"{hashlib.sha256(rendered).hexdigest()}  {relative}\n"
        from unbake.typemap.mapping import compiler_inputs

        config_content = toml.dumps(data).encode()
        mapped = compiler_inputs(project, config_content)
        previous_map = mapped[0].read_bytes() if mapped else None
        try:
            compiler_files.atomic_bytes(recipe, rendered)
            compiler_files.atomic_bytes(manifest, "".join(lines).encode())
            if mapped is not None:
                compiler_files.atomic_bytes(mapped[0], mapped[1])
            compiler_files.atomic_bytes(path, config_content)
        except BaseException:
            compiler_files.atomic_bytes(recipe, previous_recipe)
            compiler_files.atomic_bytes(manifest, previous_manifest)
            compiler_files.atomic_bytes(path, content)
            if mapped is not None and previous_map is not None:
                compiler_files.atomic_bytes(mapped[0], previous_map)
            raise
    return config.load(project.root)
