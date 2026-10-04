"""Header overlays and content identities shared by draft, try and submit."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
from dataclasses import replace
from functools import partial
from pathlib import Path
from typing import Any, cast

from unbake.layout import split
from unbake.project import build, makefile
from unbake.config import Held, Host, Project, load_policy
from unbake.project.flow import WorkManifest
from unbake.project_tools import atomic as atomic_files
from unbake.project_tools.compile_identity import driver_content, driver_names, selected_pins


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


def source_headers(project: Project, source: Path) -> dict[str, str]:
    """Bind transitive include inputs, without binding unused project headers."""
    roots = list(project.include)
    flags = [
        flag for version in project.versions for flag in makefile.flags(project, version, project.src / source.name)
    ]
    if project.compiler_for(project.src / source.name).kind == "sn64":
        flags.extend(makefile.recipe(project).cppflags)
    forced = []
    options = iter(flags)
    for flag in options:
        if flag in {"-I", "-isystem", "-iquote"}:
            value = next(options, "")
        elif flag.startswith("-I") and len(flag) > 2:
            value = flag[2:]
        elif flag in {"-include", "-imacros"}:
            forced.append(next(options, ""))
            continue
        else:
            continue
        root = Path(value)
        roots.append(root if root.is_absolute() else project.root / root)
    inputs: dict[str, str] = {}
    pending = [source]
    for name in forced:
        dependency = next(
            (directory / name for directory in (project.root, *roots) if (directory / name).is_file()), None
        )
        if dependency is not None:
            inputs[dependency.relative_to(project.root).as_posix()] = digest(dependency.read_bytes())
            pending.append(dependency)
    visited: set[Path] = set()
    while pending:
        path = pending.pop()
        if path in visited:
            continue
        visited.add(path)
        text = path.read_text()
        for directive in re.findall(r"^\s*#\s*include\s+([^\n]+)", text, re.M):
            match = re.match(r'[<"]([^>"]+)[>"]', directive)
            if match is None:
                # A macro include can select any configured header.
                return headers(project)
            name = match[1]
            dependency = next(
                (directory / name for directory in (path.parent, *roots) if (directory / name).is_file()), None
            )
            if dependency is None:
                continue
            relative = next(
                (
                    root.relative_to(project.root) / dependency.relative_to(root)
                    for root in project.include
                    if dependency.is_relative_to(root) and root.is_relative_to(project.root)
                ),
                dependency.relative_to(project.root)
                if dependency.is_relative_to(project.root)
                else Path(dependency.name),
            )
            inputs[relative.as_posix()] = digest(dependency.read_bytes())
            pending.append(dependency)
    return inputs


def overlay(project: Project, directory: Path) -> Project:
    """Copy include inputs before running any inference that writes a header."""
    destination = directory / "overlay"
    destination.mkdir(parents=True)
    for root in project.include:
        if any(path.is_symlink() for path in root.rglob("*")):
            raise Held("draft", f"paths.include: header symlink in {root}")
        atomic_files.copytree(root, destination / root.relative_to(project.root), dirs_exist_ok=True)
    atomic_files.write(directory / "overlay.json", encoded({"base": headers(project), "edits": {}}))
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
    return replace(project, include=(*roots, *project.include), compilers=compilers, overlay_roots=roots)


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
    atomic_files.write(directory / "overlay.json", encoded(metadata))


def overlay_data(project: Project, source: Path) -> dict[str, Any]:
    path = source.parent / "overlay.json"
    if not path.is_file():
        return {"base": headers(project), "edits": {}}
    data: dict[str, Any] = json.loads(path.read_bytes())
    current = source_headers(project, source)
    directory = source.parent / "overlay"
    actual = {}
    for path in sorted(directory.rglob("*")):
        if path.is_symlink() or not path.resolve().is_relative_to(directory.resolve()):
            raise Held("try", "trial.overlay: header path escapes overlay")
        if path.is_file():
            actual[str(path.relative_to(directory))] = digest(path.read_bytes())
    if any(
        name in actual and data["base"].get(name) != value and actual[name] != value for name, value in current.items()
    ):
        raise Held(
            "try",
            "trial.overlay_stale: project headers changed; draft again",
            next_action="unbake draft "
            + shlex.quote(source.stem)
            + " --scratch "
            + shlex.quote(str(source.parent.parent.parent if source.parent.parent.name == "drafts" else source.parent)),
        )
    # A preceding publication may already have installed this exact proposal.
    # Bind its proved bytes as the current base instead of proposing it again.
    data["base"] = {**data["base"], **{name: value for name, value in current.items() if actual.get(name) == value}}
    data["edits"] = {relative: sha for relative, sha in actual.items() if data["base"].get(relative) != sha}
    return data


def compilation_project(project: Project, source: Path) -> Project:
    view = source.parent / "trial-view.json"
    if view.is_file():
        from unbake.match.staging import project_at

        return project_at(project, project.root / json.loads(view.read_bytes())["root"])
    overlay_data(project, source)
    return overlay_project(project, source.parent) if (source.parent / "overlay.json").is_file() else project


def overlay_source(project: Project, source: Path, directory: Path, root: Path) -> Path:
    """Retain an explicit include proposal beside unchanged authored bytes for submit."""
    root = root.resolve()
    if not root.is_dir() or any(path.is_symlink() for path in root.rglob("*")):
        raise Held("try", "trial.overlay_root: required include directory without symlinks")
    staged = overlay(project, directory)
    atomic_files.copytree(root, staged.include[0], dirs_exist_ok=True)
    save_overlay(project, directory)
    destination = directory / source.name
    atomic_files.copyfile(source, destination)
    return destination


def trial_view(project: Project, policy: Host, source: Path, directory: Path) -> Path:
    """Use submit's single-source fold in an entirely private compilation tree."""
    from unbake.layout.header_context import Headers
    from unbake.match import batch_fold, staging
    from unbake.match.batch import Candidate

    tree = directory / "tree"
    tree.mkdir(parents=True)
    atomic_files.copyfile(project.root / "config.toml", tree / "config.toml")
    if (project.root / "layout.toml").is_file():
        atomic_files.copyfile(project.root / "layout.toml", tree / "layout.toml")
    from unbake.layout import index

    lookup = index.path(project)
    if lookup.is_file():
        target = tree / lookup.relative_to(project.root)
        target.parent.mkdir(parents=True, exist_ok=True)
        atomic_files.copyfile(lookup, target)
    for root in (*project.include, project.root / "versions"):
        atomic_files.copytree(root, tree / root.relative_to(project.root))
    for root in (project.tools, project.roms):
        (tree / root.relative_to(project.root)).symlink_to(root, target_is_directory=True)
    staged = staging.project_at(project, tree)
    staged.src.mkdir(parents=True, exist_ok=True)
    staged.work.mkdir(parents=True, exist_ok=True)
    candidate = Candidate(
        source.stem,
        source,
        source.read_bytes(),
        digest(source.read_bytes()),
        split.holding_versions(project, source.stem),
        True,
    )
    headers = Headers.read(staged, cache_root=getattr(policy, "cache_root", None))
    original_headers = dict(headers.texts)
    folded = batch_fold._fold_one(staged, policy, headers, candidate, batch_fold.Changes())
    for path, text in headers.texts.items():
        if original_headers.get(path) == text:
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_files.text(path, text)
    result = staged.src / source.name
    atomic_files.text(result, folded.source)
    atomic_files.write(result.parent / "trial-view.json", encoded({"root": os.path.relpath(tree, project.root)}))
    return result


def compiler_identity(project: Project, policy: Host, ident: str) -> str:
    """Hash the compiler's files and drivers, excluding other units' recipes."""
    compiler = project.compilers[ident]
    paths = {compiler.cc}
    groups: dict[str, dict[str, str]] = {}
    for row in compiler.sha256.read_text().splitlines():
        fields = row.split(maxsplit=1)
        if len(fields) == 2 and not row.startswith("#"):
            name = fields[1].lstrip("*")
            groups.setdefault(str(Path(name).parent), {})[name] = fields[0]
    paths.update(
        project.root / name
        for name in selected_pins(
            groups, compiler.cc.relative_to(project.root), project.tools.relative_to(project.root), compiler.kind
        )
    )
    drivers = list(driver_names("cc", compiler.kind == "sn64"))
    settings: dict[str, Any] = {"kind": compiler.kind}
    if compiler.kind == "sn64":
        import abumasn64

        recipe = makefile.recipe(project)
        settings.update(asflags=recipe.sn64_asflags, cppflags=recipe.cppflags)
        for value, field in ((str(compiler.as_), "as"), (recipe.cpp or "", "cpp")):
            executable = makefile.host_executable(policy, value, field)
            path = Path(shutil.which(executable) or executable)
            paths.add(path if path.is_absolute() else project.root / path)
        assert abumasn64.__file__ is not None
        paths.update(Path(abumasn64.__file__).parent.glob("*.py"))
    paths.update(project.tools / name for name in drivers if (project.tools / name).is_file())
    paths.update(
        Path(makefile.__file__).with_name(name) if name == "cache.py" else makefile.TEMPLATES / name for name in drivers
    )
    for path in (*paths, compiler.sha256):
        if not path.is_file():
            raise Held("try", f"trial.compiler.{ident}: missing {path}")
    from unbake.cache import memo

    # Installed compilers and drivers are replaced, never edited in place:
    # file identity and size select the content digest within one process.
    signature = tuple((path, (stat := path.stat()).st_ino, stat.st_size, stat.st_mtime_ns) for path in sorted(paths))
    return memo(
        "compiler.identity",
        (project.root, json.dumps(settings, sort_keys=True), signature),
        lambda: _identity(project, paths, settings),
    )


def _identity(project: Project, paths: set[Path], settings: dict[str, Any]) -> str:
    # The manifest includes build.json, which changes with unrelated unit choices.
    # Bind the selected compiler, drivers and effective compilation settings instead.
    return digest(
        encoded(
            {
                "settings": settings,
                "files": {
                    path.relative_to(project.root).as_posix()
                    if path.is_relative_to(project.root)
                    else "host:" + path.parent.name + "/" + path.name: digest(
                        driver_content(path) if path.suffix == ".py" else path.read_bytes()
                    )
                    for path in sorted(paths)
                },
            }
        )
    )


def current_trial(
    project: Project, policy: Host, source: Path, versions: list[str], recorded: dict[str, Any]
) -> WorkManifest:
    """Validate source-scoped inputs and reconstruct its measured compiler view."""
    from unbake.project import compiler_choice

    if "compiler_evidence" not in recorded:
        raise Held("submit", "trial.receipt: current source-scoped receipt required; run unbake try")
    evidence = recorded["compiler_evidence"]
    if evidence:
        if evidence.get("configured") != project.compiler_reference(source.stem):
            raise Held("submit", "submit.compiler_candidates: configured compiler changed since latest try")
        project = compiler_choice.selected(project, source.stem, evidence["selected"])
    current = identity(project, source, versions, policy=policy)
    for key in (
        "schema",
        "project_id",
        "source_sha256",
        "overlay_sha256",
        "names_from",
        "versions",
        "rom_sha1",
        "compiler_sha256",
        "flags",
        "target_sha256",
        "layout_sha256",
        "entries",
    ):
        if recorded.get(key) != current[key]:
            raise Held("submit", f"submit.{key}: changed since latest try")
    current["compiler_evidence"] = evidence
    header_edits(project, current)
    return current


def identity(
    project: Project,
    source: Path,
    versions: list[str],
    *,
    pinned: dict[str, tuple[Path, Path]] | None = None,
    policy: Host | None = None,
) -> WorkManifest:
    """Capture this source's build inputs and every immutable holding target."""
    overlay_inputs = overlay_data(project, source)
    configured = project.compiler_for(project.src / source.name)
    selected_policy = policy if policy is not None else load_policy()
    compiler_hash = compiler_identity(project, selected_policy, configured.id)
    targets = {}
    entry_inventory = {}
    layouts = {}
    flags = {}
    for version in versions:
        cartridge = project.version(version)
        owners = [row for row in split.functions(project, version) if source.stem in row.aliases]
        _, symbols = split.symbols(cartridge.symbols)
        identifiers = set(re.findall(r"[A-Za-z_]\w*", source.read_text()))
        layouts[version] = {
            "owners": [
                {"start": r.start, "end": r.end, "address": r.address, "path": r.path, "kind": r.kind} for r in owners
            ],
            "symbols": {name: value[0] for name, value in symbols.items() if name in identifiers},
        }
        providers = project.build / "setup" / (version + ".json")
        if providers.is_file():
            from unbake.cache import parsed

            provided = parsed("setup.providers", providers, partial(_providers, providers))
            layouts[version]["providers"] = [r for r in provided if source.stem in r.get("owners", [])]
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
        from unbake.decomp import trial_entries

        if pinned is None:
            target = trial_entries.target(project, selected_policy, source, version, generation, target)
        entry_inventory[version] = trial_entries.inventory(project, selected_policy, source, version)
        targets[version] = digest(target.read_bytes())
    destination = source if source.is_relative_to(project.root) else project.drafts / source.stem / source.name
    return cast(
        WorkManifest,
        {
            "schema": 1,
            "project_id": project.id,
            "rom_sha1": {v: project.version(v).baserom_sha1 for v in versions},
            "kind": "function",
            "subject": source.stem,
            "source": str(destination.relative_to(project.root)),
            "source_sha256": digest(source.read_bytes()),
            "overlay_sha256": digest(
                encoded(
                    {
                        "headers": source_headers(compilation_project(project, source), source),
                        "edits": overlay_inputs["edits"],
                    }
                )
            ),
            "names_from": project.names_from,
            "versions": versions,
            "compiler_sha256": {configured.id: compiler_hash},
            "compiler_evidence": {},
            "flags": flags,
            "target_sha256": targets,
            "layout_sha256": digest(encoded(layouts)),
            "needs": {"headers": overlay_inputs["edits"]},
            "entries": entry_inventory,
            "evidence": {
                "overlay_directory": os.path.relpath(source.parent, project.root)
                if (source.parent / "overlay.json").is_file()
                else None,
            },
        },
    )


def _providers(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = json.loads(path.read_bytes()).get("providers", [])
    return rows


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
        staged = project.root / directory / "overlay" / relative
        if digest(staged.read_bytes()) != expected:
            raise Held("submit", f"submit.overlay_sha256: {relative} changed since try")
        edits.append(split.Edit(path, path.read_text() if path.exists() else "", staged.read_text(), project.versions))
    return edits
