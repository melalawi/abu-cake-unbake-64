"""Materialize an isolated project with its current warm build generations."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from collections.abc import Sequence
from dataclasses import fields
from pathlib import Path

from unbake.project import build, compiler_files, config, toolchain
from unbake.project.config import Held, Policy, Project


def project_relative(project: Project, path: Path) -> Path:
    try:
        relative = path.relative_to(project.root)
    except ValueError as error:
        raise Held("clone", f"{path}: required project-local input") from error
    if not relative.parts or ".." in relative.parts or relative.parts[0] == ".git":
        raise Held("clone", f"{path}: required project-local input")
    return relative


def require(path: Path, *, directory: bool = False) -> None:
    if not (path.is_dir() if directory else path.is_file()):
        raise Held("clone", f"{path}: missing {'directory' if directory else 'file'}")


def copy_regular(source: Path, destination: Path, ancestors: frozenset[Path] = frozenset()) -> None:
    """Read links at the source, unlink destination links before any writes."""
    resolved = source.resolve(strict=True)
    if resolved in ancestors:
        raise Held("clone", f"{source}: cyclic directory link")
    if destination.is_symlink():
        destination.unlink()
    if resolved.is_dir():
        if destination.exists() and not destination.is_dir():
            destination.unlink()
        destination.mkdir(parents=True, exist_ok=True)
        for child in source.iterdir():
            copy_regular(child, destination / child.name, ancestors | {resolved})
        shutil.copystat(source, destination)
    else:
        require(source)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)


def git(root: Path, *arguments: str) -> bytes:
    result = subprocess.run(["git", "-C", str(root), *arguments], capture_output=True)
    if result.returncode:
        raise Held("clone", f"git {' '.join(arguments)}: {result.stderr.decode(errors='replace').strip()}")
    return result.stdout


def isolated_policy(policy: Policy, destination: Path) -> str:
    values: dict[str, object] = {field.name: getattr(policy, field.name) for field in fields(policy)}
    values.update(cache_root=destination / ".unbake/cache", state_root=destination / ".unbake/state")
    return "".join(
        f"{name} = {json.dumps(str(value) if isinstance(value, Path) else value)}\n" for name, value in values.items()
    )


def refresh_checksums(project: Project) -> None:
    """Refresh helper hashes while retaining verified compiler pins."""
    manifest = project.tools / "compiler.sha256"
    rows = []
    for line in manifest.read_text().splitlines():
        parts = line.split(maxsplit=1)
        if len(parts) != 2 or not re.fullmatch(r"[0-9a-fA-F]{64}", parts[0]):
            raise Held("clone", f"{manifest}: invalid checksum entry {line!r}")
        expected, name = parts
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts:
            raise Held("clone", f"{manifest}: required project-relative checksum input {name}")
        path = project.root / relative
        require(path)
        actual = compiler_files.sha(path)
        if path.parent != project.tools and actual != expected.lower():
            raise Held("clone", f"{path}: compiler checksum expected {expected}, found {actual}")
        rows.append(f"{actual}  {name}\n")
    manifest.write_text("".join(rows))


def create(project: Project, policy: Policy, destination: Path, versions: Sequence[str]) -> Project:
    """Clone Git history, then copy live build inputs and receipts without hardlinks."""
    destination = destination.expanduser().absolute()
    if any(path.is_symlink() for path in (destination, *destination.parents)):
        raise Held("clone", f"{destination}: destination has a symlink component")
    destination = destination.resolve()
    if destination.exists():
        raise Held("clone", f"{destination}: destination already exists")
    if destination.is_relative_to(project.root) or project.root.is_relative_to(destination):
        raise Held("clone", f"{destination}: destination overlaps source project")
    if not versions:
        raise Held("clone", "--version: required nonempty selection")
    if len(set(versions)) != len(versions):
        raise Held("clone", "--version: duplicate VERSION")
    inputs = [project.src, project.tools, *project.include]
    for path in inputs:
        project_relative(project, path)
        require(path, directory=True)
    require(project.root / "Makefile")
    require(project.tools / "compiler.sha256")
    generations = {}
    for name in versions:
        version = project.version(name)
        for path in (version.baserom, version.split, version.symbols):
            project_relative(project, path)
            require(path)
        actual = hashlib.sha1(version.baserom.read_bytes()).hexdigest()
        if actual != version.baserom_sha1:
            raise Held("clone", f"{version.baserom}: sha1 expected {version.baserom_sha1}, found {actual}")
        generation = build.current_generation(project, name)
        if generation.parent != project.root / "build" or not re.fullmatch(re.escape(name) + r"\.\d+", generation.name):
            raise Held("clone", f"{generation}: required build/{name}.N generation")
        for filename in (".split.mk", ".extract-key", project.name + ".elf", f"{project.name}.{name}.z64"):
            require(generation / filename)
        assembly = project.asm / name
        project_relative(project, assembly)
        require(assembly, directory=True)
        generations[name] = generation
    for ident in project.compilers:
        toolchain.verify(project.tools / ident, toolchain.specification(ident))
    tracked = git(project.root, "ls-files", "-z").decode().split("\0")
    for name in filter(None, tracked):
        require(project.root / name)
    destination.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        ["git", "clone", "--no-hardlinks", "--", str(project.root), str(destination)], capture_output=True, text=True
    )
    if result.returncode:
        raise Held("clone", f"git clone: {result.stderr.strip()}")
    # Materialize checkout links first, so nested copies cannot traverse them.
    for child in destination.iterdir():
        if child.name != ".git":
            materialize_links(child)
    for name in filter(None, tracked):
        copy_regular(project.root / name, destination / name)
    for path in inputs:
        copy_regular(path, destination / project_relative(project, path))
    for name, generation in generations.items():
        version = project.version(name)
        for path in (version.baserom, version.split, version.symbols, project.asm / name):
            copy_regular(path, destination / project_relative(project, path))
        target = destination / "build" / generation.name
        copy_regular(generation, target)
        link = destination / "build" / name
        if link.is_symlink():
            link.unlink()
        elif link.exists():
            shutil.rmtree(link)
        link.symlink_to(target.name, target_is_directory=True)
    cloned = config.load(destination)
    for path in (cloned.src, cloned.tools, cloned.asm, *cloned.include):
        project_relative(cloned, path)
    policy_path = cloned.tools / "clone-policy.toml"
    if policy_path.is_symlink():
        policy_path.unlink()
    policy_path.write_text(isolated_policy(policy, destination))
    makefile = destination / "Makefile"
    original_stat = makefile.stat()
    local_policy = project_relative(cloned, policy_path).as_posix()
    makefile.write_text(f"export UNBAKE_POLICY := $(abspath {local_policy})\n" + makefile.read_text())
    os.utime(makefile, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
    refresh_checksums(cloned)
    return cloned


def materialize_links(path: Path) -> None:
    if path.is_symlink():
        target = path.resolve(strict=True)
        path.unlink()
        copy_regular(target, path)
    elif path.is_dir():
        for child in path.iterdir():
            materialize_links(child)
