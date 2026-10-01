"""Header overlays and content identities shared by draft, try and submit."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

from unbake.layout import split
from unbake.project import build, makefile
from unbake.project.config import Held, Policy, Project, load_policy
from unbake.project.flow import WorkManifest


def digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def encoded(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def headers(project: Project) -> dict[str, str]:
    return {
        str(path.relative_to(project.root)): digest(path.read_bytes())
        for root in project.include
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def overlay(project: Project, directory: Path) -> Project:
    """Copy include inputs before running any inference that writes a header."""
    destination = directory / "overlay"
    destination.mkdir(parents=True)
    for root in project.include:
        if any(path.is_symlink() for path in root.rglob("*")):
            raise Held("draft", f"paths.include: header symlink in {root}")
        shutil.copytree(root, destination / root.relative_to(project.root), dirs_exist_ok=True)
    (directory / "overlay.json").write_bytes(encoded({"base": headers(project), "edits": {}}))
    return overlay_project(project, directory)


def overlay_project(project: Project, directory: Path) -> Project:
    roots = tuple(directory / "overlay" / root.relative_to(project.root) for root in project.include)

    def remap(value: str) -> str:
        for root, staged in zip(project.include, roots, strict=True):
            relative = str(root.relative_to(project.root))
            if value == relative or value.startswith(relative + "/"):
                return str(staged / Path(value).relative_to(relative))
            if value == str(root) or value.startswith(str(root) + "/"):
                return str(staged / Path(value).relative_to(root))
        return value

    compilers = {
        ident: replace(
            compiler,
            cflags=tuple(
                "-I" + remap(f[2:]) if f.startswith("-I") and len(f) > 2 else remap(f) for f in compiler.cflags
            ),
        )
        for ident, compiler in project.compilers.items()
    }
    return replace(project, include=roots, compilers=compilers)


def save_overlay(project: Project, directory: Path) -> None:
    metadata = json.loads((directory / "overlay.json").read_bytes())
    edits = {}
    for root in project.include:
        staged = directory / "overlay" / root.relative_to(project.root)
        for path in sorted(staged.rglob("*")):
            if path.is_file():
                relative = str(path.relative_to(directory / "overlay"))
                if digest(path.read_bytes()) != metadata["base"].get(relative):
                    edits[relative] = digest(path.read_bytes())
    metadata["edits"] = edits
    (directory / "overlay.json").write_bytes(encoded(metadata))


def overlay_data(project: Project, source: Path) -> dict[str, Any]:
    path = source.parent / "overlay.json"
    if not path.is_file():
        return {"base": headers(project), "edits": {}}
    data: dict[str, Any] = json.loads(path.read_bytes())
    if data.get("base") != headers(project):
        raise Held("try", "trial.overlay_stale: project headers changed; draft again")
    directory = source.parent / "overlay"
    actual = {}
    for path in sorted(directory.rglob("*")):
        if path.is_symlink() or not path.resolve().is_relative_to(directory.resolve()):
            raise Held("try", "trial.overlay: header path escapes overlay")
        if path.is_file():
            actual[str(path.relative_to(directory))] = digest(path.read_bytes())
    if any(relative not in actual for relative in data["base"]):
        raise Held("try", "trial.overlay: staged header deletion requires an explicit edit")
    data["edits"] = {relative: sha for relative, sha in actual.items() if data["base"].get(relative) != sha}
    return data


def compilation_project(project: Project, source: Path) -> Project:
    overlay_data(project, source)
    return overlay_project(project, source.parent) if (source.parent / "overlay.json").is_file() else project


def identity(
    project: Project,
    source: Path,
    versions: list[str],
    *,
    pinned: dict[str, tuple[Path, Path]] | None = None,
    policy: Policy | None = None,
) -> WorkManifest:
    """Capture exact inputs, including generation and every holding target."""
    overlay_inputs = overlay_data(project, source)
    configured = project.compiler_for(project.src / source.name)
    assembler = configured.as_
    if str(assembler).startswith("policy:"):
        selected_policy = policy if policy is not None else load_policy()
        assembler = Path(makefile.host_executable(selected_policy, str(assembler), "as"))
    compiler_files = [configured.cc, assembler, configured.sha256]
    for field, path in zip(("cc", "as", "sha256"), compiler_files, strict=True):
        if not path.is_file():
            raise Held("try", f"trial.compiler.{configured.id}.{field}: missing {path}")
    compiler_hash = digest(
        encoded(
            {
                str(p.relative_to(project.root) if p.is_relative_to(project.root) else p): digest(p.read_bytes())
                for p in compiler_files
                if p.is_file()
            }
        )
    )
    targets, generations = {}, {}
    layouts = {}
    flags = {}
    for version in versions:
        cartridge = project.version(version)
        layouts[version] = {
            str(p.relative_to(project.root) if p.is_relative_to(project.root) else p): digest(p.read_bytes())
            for p in (cartridge.split, cartridge.symbols)
        }
        flags[version] = list(makefile.flags(project, version, project.src / source.name))
        if pinned is not None:
            generation, target = pinned[version]
        else:
            generation = build.current_generation(project, version)
            _, _, segments = split.layout(cartridge.split)
            rows = [
                r
                for segment in segments
                for r in segment.rows
                if r.kind in ("asm", "c") and Path(r.path).name == source.stem
            ]
            if len(rows) != 1:
                raise Held("try", f"trial.ownership: {source.stem} requires one owner in {version}")
            row = rows[0]
            target = generation / "obj" / ("src" if row.kind == "c" else "asm") / (row.path + ".o")
        if not target.is_file():
            raise Held("try", f"trial.target_sha256: target {target} is missing")
        targets[version] = digest(target.read_bytes())
        generations[version] = digest(
            encoded(
                {
                    "path": str(generation),
                    "target": targets[version],
                    "inputs": {
                        p.name: digest(p.read_bytes())
                        for p in (
                            generation / ".extract-key",
                            generation / ".split.mk",
                            generation / f"{project.name}.ld",
                        )
                        if p.is_file()
                    },
                }
            )
        )
    for path in (
        project.build / "setup/layout.json",
        project.root / "docs/setup/layout.json",
        project.build / "types/database.json",
        *(project.root / "docs/setup" / (version + ".json") for version in versions),
    ):
        if path.is_file():
            layouts[str(path.relative_to(project.root))] = {"sha256": digest(path.read_bytes())}
    destination = source if source.is_relative_to(project.root) else project.drafts / source.stem / source.name
    return cast(
        WorkManifest,
        {
            "schema": 1,
            "project_id": project.id,
            "workspace_id": project.workspace_id,
            "rom_sha1": {v: project.version(v).baserom_sha1 for v in versions},
            "kind": "function",
            "subject": source.stem,
            "source": str(destination.relative_to(project.root)),
            "source_sha256": digest(source.read_bytes()),
            "overlay_sha256": digest(encoded(overlay_inputs)),
            "names_from": project.names_from,
            "versions": versions,
            "compiler_sha256": {configured.id: compiler_hash},
            "flags": flags,
            "target_sha256": targets,
            "generation_sha256": generations,
            "layout_sha256": digest(encoded(layouts)),
            "needs": {"headers": overlay_inputs["edits"]},
            "evidence": {
                "config_sha256": digest((project.root / "config.toml").read_bytes()),
                "overlay_directory": str(source.parent) if (source.parent / "overlay.json").is_file() else None,
            },
        },
    )


def persist(project: Project, manifest: WorkManifest) -> None:
    subject = manifest["subject"]
    if not re.fullmatch(r"[A-Za-z_]\w*", subject):
        raise Held("draft", "draft.function: expected a C identifier")
    directory = project.drafts / subject
    directory.mkdir(parents=True, exist_ok=True)
    from unbake.match.common import atomic

    atomic(directory / "manifest.json", encoded(manifest))


def header_edits(project: Project, manifest: WorkManifest) -> list[split.Edit]:
    directory = manifest["evidence"].get("overlay_directory")
    edits = []
    for relative, expected in manifest["needs"].get("headers", {}).items():
        path = project.root / relative
        if not any(path.resolve().is_relative_to(root.resolve()) for root in project.include):
            raise Held("submit", f"submit.overlay: {relative} is outside paths.include")
        if directory is None:
            raise Held("submit", "submit.overlay: missing staged headers")
        staged = Path(directory) / "overlay" / relative
        if digest(staged.read_bytes()) != expected:
            raise Held("submit", f"submit.overlay_sha256: {relative} changed since try")
        edits.append(split.Edit(path, path.read_text() if path.exists() else "", staged.read_text(), project.versions))
    return edits
