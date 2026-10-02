"""Prove isolated whole-name edits before atomically publishing files and ROMs."""

from __future__ import annotations

import hashlib
import tempfile
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path

from unbake.match import staging
from unbake.match.common import atomic
from unbake.match.publication import swap
from unbake.project import build, config, makefile
from unbake.project.clone import refresh_checksums
from unbake.project.config import Held, Policy, Project


@dataclass(frozen=True)
class Change:
    path: Path
    before: bytes | None
    after: bytes | None


def apply(
    project: Project, policy: Policy, changes: list[Change], *, verify: Callable[[], None] | None = None
) -> list[build.BuildResult]:
    """Canonical inputs and current links stay untouched until all ROMs prove."""
    if not changes:
        return []
    for change in changes:
        if not change.path.resolve().is_relative_to(project.root.resolve()) or change.path.is_symlink():
            raise Held("split", f"split.rename.path: {change.path}: outside project or symlink")
        if (change.path.read_bytes() if change.path.exists() else None) != change.before:
            raise Held("split", f"split.rename.stale: {change.path}: changed since preview")
    project.work.mkdir(parents=True, exist_ok=True)
    generations: dict[str, Path] = {}
    published = False
    with ExitStack() as holds, tempfile.TemporaryDirectory(prefix="rename-", dir=project.work) as temporary:
        tree = Path(temporary) / "project"
        fingerprint = staging.fingerprint(project, project.root)
        staging.copy_tree(project, project.root, tree)
        local_policy = project.tools / "clone-policy.toml"
        if local_policy.is_file():
            atomic(tree / local_policy.relative_to(project.root), local_policy.read_bytes())
        current = {}
        with build.lock(project):
            for version in project.versions:
                current[version] = holds.enter_context(build.pin(build.current_generation(project, version)))
        try:
            for change in changes:
                path = tree / change.path.relative_to(project.root)
                if change.after is None:
                    path.unlink(missing_ok=True)
                else:
                    atomic(path, change.after)
            staged = config.load(tree)
            # Regenerate compiler-unit recipes from the renamed explicit config.
            generated = makefile.render(staged)
            extra = []
            for relative, text in generated.items():
                destination = project.root / relative
                staged_path = tree / relative
                content = text.encode()
                if staged_path.exists() and staged_path.read_bytes() == content:
                    continue
                extra.append(Change(destination, destination.read_bytes() if destination.exists() else None, content))
                atomic(staged_path, content)
            refresh_checksums(staged)
            checksum = staged.tools / "compiler.sha256"
            destination = project.root / checksum.relative_to(tree)
            content = checksum.read_bytes()
            if destination.read_bytes() != content:
                extra.append(Change(destination, destination.read_bytes(), content))
            all_changes = [*changes, *extra]
            for version in project.versions:
                generations[version] = staging.generation(project, version, current[version], holds)
                staging.chunk_stale_sources(generations[version], staged.tools)
            results = build.build(
                project, policy, list(project.versions), tree=tree, generation_for=generations.__getitem__
            )
            ordered = []
            for version in project.versions:
                result = results.get(version)
                if result is None or not result.ok:
                    detail = (
                        "missing build result" if result is None else staging.compare_failure(staged, version, result)
                    )
                    if result is not None and result.log.is_file():
                        diagnostics = [
                            line
                            for line in result.log.read_text().splitlines()
                            if "HELD(" in line
                            or ": error:" in line
                            or line.startswith(("Traceback", "FileNotFoundError", "RuntimeError", "ValueError"))
                        ]
                        detail += "; " + "; ".join(diagnostics[:8])
                    raise Held("split", f"split.rename.sha1.{version}: {detail}")
                rom = result.generation / f"{project.name}.{version}.z64"
                digest = hashlib.sha1(rom.read_bytes()).hexdigest()
                if digest != project.version(version).baserom_sha1:
                    raise Held(
                        "split",
                        f"split.rename.sha1.{version}: expected {project.version(version).baserom_sha1}, got {digest}",
                    )
                ordered.append(result)
            # Extraction may materialize renamed asm/rodata paths in the proof
            # tree. The published generation must keep those exact inputs.
            changed_paths = {change.path for change in all_changes}
            for path in sorted(staged.asm.rglob("*")):
                if not path.is_file():
                    continue
                destination = project.root / path.relative_to(tree)
                before = destination.read_bytes() if destination.exists() else None
                after = path.read_bytes()
                if before != after and destination not in changed_paths:
                    all_changes.append(Change(destination, before, after))
            with build.lock(project):
                if staging.fingerprint(project, project.root) != fingerprint:
                    raise Held("split", "split.rename.stale: project inputs changed during proof")
                for change in all_changes:
                    before = change.path.read_bytes() if change.path.exists() else None
                    if before != change.before:
                        raise Held("split", f"split.rename.stale: {change.path}: changed during proof")
                for version, generation in current.items():
                    if build.current_generation(project, version).resolve() != generation.resolve():
                        raise Held("split", f"split.rename.stale: {version}: generation changed during proof")
                if verify is not None:
                    verify()
                written, swapped = [], []
                try:
                    for change in all_changes:
                        if change.after is None:
                            change.path.unlink(missing_ok=True)
                        else:
                            atomic(change.path, change.after)
                        written.append(change)
                    for version, generation in generations.items():
                        swap(project.build_link(version), generation)
                        swapped.append(version)
                except BaseException:
                    for version in reversed(swapped):
                        swap(project.build_link(version), current[version])
                    for change in reversed(written):
                        if change.before is None:
                            change.path.unlink(missing_ok=True)
                        else:
                            atomic(change.path, change.before)
                    raise
            published = True
            return ordered
        finally:
            holds.close()
            if not published:
                for generation in generations.values():
                    build.discard_generation(generation)
